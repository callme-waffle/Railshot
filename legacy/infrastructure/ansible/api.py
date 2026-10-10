#!/usr/bin/env python3
"""Private localhost Ansible jobs API. One worker; no public target registration."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import sys
import threading
import time
from urllib.parse import urlsplit

import run as ansible
import inputs
import database

sys.path.insert(0, str(ansible.ROOT / 'ci/scripts'))
from storage import durable_write


class APIError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def parsed(raw):
    return json.loads(raw, object_pairs_hook=ansible.unique_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def private_json(path):
    ansible.private_file(path, identity=True)
    raw = Path(path).read_bytes()
    if len(raw) > ansible.MAX_BYTES:
        raise ValueError('private configuration exceeds size limit')
    return parsed(raw)


def private_directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('absolute private state directory required')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private state directory required')
    return path


def execute_request(request, state_dir):
    """Use the same bounded adapter as the CLI without an orphanable nested CLI."""
    log_path = state_dir / 'http-jobs' / (hashlib.sha256(request['request_id'].encode()).hexdigest() + '.log')
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as log:
        runner = database.run if request['operation'] == 'database.configure' else ansible.run
        result = runner(request, state_dir=state_dir)
        log.write(encoded({'status': result['status'], 'stage': result['stage']}))
    return result


class Jobs:
    def __init__(self, targets_file, state_dir, executor=execute_request):
        config = private_json(targets_file)
        if (not isinstance(config, dict) or not {'version', 'targets'} <= set(config)
                or set(config) - {'version', 'targets', 'database_profiles'} or config['version'] != 1):
            raise ValueError('registered target configuration required')
        self.database_profiles = config.get('database_profiles', {})
        if not isinstance(self.database_profiles, dict) or any(
                not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', key) or not isinstance(path, str) or not Path(path).is_absolute()
                for key, path in self.database_profiles.items()):
            raise ValueError('invalid registered database profile')
        self.targets = config['targets']
        if not isinstance(self.targets, dict) or not self.targets:
            raise ValueError('registered target allowlist required')
        for target_id, target in self.targets.items():
            common = {'ssh', 'timeout_seconds', 'purpose'}
            openstack = {'server_file', 'resource_id', 'project_id', 'management_network',
                         'placement', 'architecture', 'initialization'}
            required = openstack if isinstance(target, dict) and 'server_file' in target else {'descriptor_file'}
            optional = {'management_endpoint'} if required == openstack else set()
            if (not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', target_id) or not isinstance(target, dict)
                    or set(target) - (required | common | optional) or not (required | {'ssh'}) <= set(target)
                    or target.get('purpose', 'runtime') not in ('runtime', 'database')):
                raise ValueError('invalid registered target')
        self.state_dir = private_directory(state_dir)
        self.directory = private_directory(self.state_dir / 'http-jobs')
        self.instance_lock = os.fdopen(os.open(self.directory / 'api.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a')
        try:
            fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.instance_lock.close()
            raise
        self.lock, self.active, self.executor = threading.Lock(), None, executor
        self.records = {}
        try:
            for path in self.directory.glob('*.json'):
                record = private_json(path)
                required = {'request_id', 'target_id', 'operation', 'request_sha256', 'status',
                            'created_at', 'updated_at', 'result', 'error'}
                if (not isinstance(record, dict) or not required <= set(record) or set(record) - required - {'lock_keys'}
                        or not isinstance(record.get('request_id'), str)
                        or path.name != self.path(record['request_id']).name
                        or record.get('status') not in {'queued', 'running', 'succeeded', 'failed', 'blocked', 'unknown'}):
                    raise ValueError('invalid persisted job record')
                self.records[record['request_id']] = record
                if record['status'] in {'queued', 'running', 'unknown'}:
                    self.reconcile(record)
            self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ansible')
        except BaseException:
            self.instance_lock.close()
            raise

    def path(self, request_id):
        return self.directory / (hashlib.sha256(request_id.encode()).hexdigest() + '.json')

    def save(self, record):
        record['updated_at'] = time.time()
        durable_write(self.path(record['request_id']), encoded(record))
        self.records[record['request_id']] = record

    @staticmethod
    def public(record):
        return {key: record[key] for key in ('request_id', 'target_id', 'operation', 'status',
                                             'created_at', 'updated_at', 'result', 'error')}

    def prepare(self, body):
        try:
            parameters = inputs.validate_job(body)
        except (ValueError, TypeError, KeyError):
            raise APIError('INVALID_JOB_REQUEST')
        if body['operation'] == 'database.configure':
            plan, request = self.database_request(body)
            if request is None:
                raise APIError(plan['blockers'][0]['code'], 501)
            return request
        request = self.resolve(body['target_id'], body['request_id'], body['operation'],
                               purpose='runtime' if body['operation'] == 'runtime.install' else None)
        if parameters:
            request['runtime'] = parameters
        try:
            return ansible.validate(request)
        except ValueError:
            raise APIError('INVALID_JOB_REQUEST') from None

    def resolve(self, target_id, request_id, operation='guest.check', *, purpose=None):
        target = self.targets.get(target_id)
        if target is None:
            raise APIError('TARGET_NOT_REGISTERED', 404)
        if purpose is not None and target.get('purpose', 'runtime') != purpose:
            raise APIError('TARGET_PURPOSE_MISMATCH', 400)
        try:
            if 'server_file' in target:
                request = ansible.from_openstack(private_json(target['server_file']),
                    request_id=request_id, operation=operation, target_id=target_id,
                    **{key: target[key] for key in ('resource_id', 'project_id', 'management_network',
                       'placement', 'architecture', 'initialization', 'ssh')},
                    timeout_seconds=target.get('timeout_seconds', 1200))
                if 'management_endpoint' in target:
                    endpoint = target['management_endpoint']
                    if not isinstance(endpoint, str):
                        raise ValueError('invalid registered management endpoint')
                    node = request['inventory']['control_plane'][0]
                    host = node['ssh'].get('connect_host', node['private_ipv4'])
                    url = urlsplit(endpoint)
                    if url.scheme != 'https' or not url.port or endpoint != f'https://{host}:{url.port}':
                        raise ValueError('management endpoint must match the registered SSH host')
                return request
            descriptor = private_json(target['descriptor_file'])
            if descriptor.get('target_id') != target_id:
                raise ValueError('target binding mismatch')
            return ansible.from_descriptor(descriptor, request_id=request_id, operation=operation,
                                           ssh=target['ssh'], timeout_seconds=target.get('timeout_seconds', 1200))
        except (ValueError, OSError, TypeError, AttributeError):
            raise APIError('TARGET_CONFIGURATION_INVALID', 503) from None

    def preview(self, body):
        try:
            parameters = inputs.validate_job(body)
            if body['operation'] == 'database.configure':
                return self.database_request(body)[0]
            return inputs.runtime_plan(self.prepare(body))
        except (ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, APIError):
                raise
            raise APIError('INVALID_JOB_REQUEST') from None

    def database_request(self, body):
        try:
            return database.prepare(body, self.database_profiles, lambda target_id:
                self.resolve(target_id, body['request_id'], purpose='database'))
        except database.ProfileError as exc:
            raise APIError(exc.code, exc.status) from None
        except (ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, APIError):
                raise
            raise APIError('INVALID_JOB_REQUEST') from None

    def submit(self, body):
        request = self.prepare(body)
        digest = hashlib.sha256(encoded(request)).hexdigest()
        with self.lock:
            prior = self.records.get(request['request_id'])
            if prior:
                if prior['request_sha256'] != digest:
                    raise APIError('REQUEST_ID_CONFLICT', 409)
                if prior['request_id'] != self.active:
                    self.reconcile(prior)
                return (202 if prior['status'] in ('queued', 'running') else 200), self.public(prior)
            for prior in self.records.values():
                # Old records without physical identities cannot safely exclude an alias.
                overlap = not prior.get('lock_keys') or set(ansible.normalize_lock_keys(prior['lock_keys'])) & set(ansible.lock_keys(request))
                if overlap and prior['status'] == 'unknown':
                    self.reconcile(prior)
                    if prior['status'] == 'unknown':
                        raise APIError('TARGET_RECONCILE_REQUIRED', 409)
            # ponytail: one admitted job, no unbounded queue; add workers only with isolated executors.
            if self.active is not None:
                raise APIError('EXECUTOR_BUSY', 409)
            record = {**{key: body[key] for key in ('request_id', 'target_id', 'operation')},
                      'request_sha256': digest, 'status': 'queued', 'created_at': time.time(),
                      'updated_at': time.time(), 'result': None, 'error': None,
                      'lock_keys': ansible.lock_keys(request)}
            self.save(record)  # Durable acceptance precedes any executor dispatch.
            self.active = body['request_id']
            try:
                self.pool.submit(self.work, record, request)
            except RuntimeError:
                self.active = None
                record.update(status='blocked', error={'code': 'EXECUTOR_UNAVAILABLE', 'outcome_unknown': False})
                self.save(record)
                raise APIError('EXECUTOR_UNAVAILABLE', 503) from None
            return 202, self.public(record)

    def finish(self, record, result):
        if (not isinstance(result, dict) or result.get('schema_version') != '1.0'
                or any(result.get(key) != record[key] for key in ('request_id', 'target_id', 'operation'))
                or result.get('status') not in ('succeeded', 'invalid', 'blocked', 'failed')
                or any(type(result.get(key)) is not bool for key in (
                    'guest_ready', 'runtime_ready', 'application_ready', 'public_http_verified'))
                or result['application_ready'] or result['public_http_verified']):
            raise ValueError('executor result binding mismatch')
        if result['status'] == 'succeeded' and (not result['guest_ready'] or (
                record['operation'] == 'runtime.install' and not result['runtime_ready'])):
            raise ValueError('executor success without readiness')
        if record['operation'] == 'database.configure' and (
                type(result.get('database_ready')) is not bool or result['runtime_ready'] or
                (result['status'] == 'succeeded' and not result['database_ready'])):
            raise ValueError('database result binding mismatch')
        error = result.get('error')
        unknown = isinstance(error, dict) and error.get('outcome_unknown') is True
        public_result = {key: result[key] for key in ('status', 'guest_ready', 'runtime_ready',
                                                    'application_ready', 'public_http_verified', 'replayed')}
        public_result['stage'] = result['stage'] if result.get('stage') in (
            'validation', 'executor_preflight', 'guest', 'runtime', 'database') else 'unknown'
        if record['operation'] == 'database.configure':
            public_result['database_ready'] = result['database_ready']
        record.update(status='unknown' if unknown else 'failed' if result['status'] == 'invalid' else result['status'],
                      result=public_result, error=None)
        if error:
            code = error.get('code', '')
            record['error'] = {'code': code if isinstance(code, str) and re.fullmatch(r'[A-Z_]{1,64}', code)
                              else 'EXECUTOR_FAILED', 'outcome_unknown': unknown}
        self.save(record)

    def reconcile(self, record):
        if record['status'] not in ('queued', 'running', 'unknown'):
            return
        native = self.state_dir / self.path(record['request_id']).name
        try:
            evidence = private_json(native)
            if evidence.get('request_sha256') != record['request_sha256'] or evidence.get('result') is None:
                raise ValueError('native completion unavailable')
            self.finish(record, evidence['result'])
            return
        except (ValueError, OSError, TypeError, KeyError, AttributeError):
            pass
        record.update(status='unknown', error={'code': 'WORKER_INTERRUPTED', 'outcome_unknown': True})
        self.save(record)

    def work(self, record, request):
        try:
            with self.lock:
                record['status'] = 'running'
                self.save(record)
            result = self.executor(request, self.state_dir)
            with self.lock:
                self.finish(record, result)
        except Exception:
            with self.lock:
                record.update(status='unknown', error={'code': 'EXECUTOR_OUTCOME_UNKNOWN', 'outcome_unknown': True})
                try:
                    self.save(record)
                except OSError:
                    pass  # Durable queued/running intent still prevents blind retry after restart.
        finally:
            with self.lock:
                self.active = None

    def get(self, request_id):
        with self.lock:
            record = self.records.get(request_id)
            if record is None:
                raise APIError('JOB_NOT_FOUND', 404)
            if request_id != self.active:
                self.reconcile(record)
            return self.public(record)

    def close(self):
        self.pool.shutdown(wait=True)
        self.instance_lock.close()


def create_server(*, targets_file, token_file, state_dir, port=4180, listen='127.0.0.1', executor=execute_request):
    if listen != '127.0.0.1':
        raise ValueError('Only private localhost listening is supported')
    ansible.private_file(token_file, identity=True)
    token = Path(token_file).read_bytes().strip()
    if not re.fullmatch(rb'[A-Za-z0-9._~-]{32,512}', token):
        raise ValueError('Bearer file must contain a private random token')
    jobs = Jobs(targets_file, state_dir, executor)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass  # Never log raw request paths, headers, bodies or native output.

        def reply(self, status, value):
            payload = encoded(value)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if status == 202:
                self.send_header('Location', '/v1/ansible/jobs/' + value['request_id'])
                self.send_header('Retry-After', '2')
            if status == 401:
                self.send_header('WWW-Authenticate', 'Bearer')
            self.end_headers()
            self.wfile.write(payload)

        def authorize(self):
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if self.headers.get_all('Host', []) not in [[host] for host in allowed]:
                raise APIError('HOST_NOT_ALLOWED', 403)
            origins = self.headers.get_all('Origin', [])
            if origins and origins not in [[f'http://{host}'] for host in allowed]:
                raise APIError('ORIGIN_NOT_ALLOWED', 403)
            headers = self.headers.get_all('Authorization', [])
            candidate = headers[0][7:].encode() if len(headers) == 1 and headers[0].startswith('Bearer ') else b''
            if not secrets.compare_digest(candidate, token):
                raise APIError('UNAUTHORIZED', 401)

        def handle_request(self):
            try:
                self.authorize()
                if self.command == 'POST' and self.path in ('/v1/ansible/jobs', '/v1/ansible/validate'):
                    if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
                        raise APIError('JSON_BODY_REQUIRED', 415)
                    lengths = self.headers.get_all('Content-Length', [])
                    if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,5}', lengths[0]):
                        raise APIError('CONTENT_LENGTH_REQUIRED', 411)
                    size = int(lengths[0])
                    if not 0 < size <= 8192:
                        raise APIError('REQUEST_TOO_LARGE', 413)
                    body = self.rfile.read(size)
                    if len(body) != size:
                        raise APIError('INVALID_JSON')
                    try:
                        value = parsed(body)
                    except (ValueError, UnicodeError):
                        raise APIError('INVALID_JSON') from None
                    status, result = ((200, jobs.preview(value)) if self.path.endswith('/validate')
                                      else jobs.submit(value))
                    self.reply(status, result)
                    return
                match = re.fullmatch(r'/v1/ansible/jobs/([A-Za-z0-9][A-Za-z0-9._-]{0,127})', self.path)
                if self.command == 'GET' and match:
                    self.reply(200, jobs.get(match[1]))
                    return
                raise APIError('NOT_FOUND', 404)
            except APIError as exc:
                self.reply(exc.status, {'error': {'code': exc.code}})
            except Exception:
                self.reply(503, {'error': {'code': 'API_UNAVAILABLE'}})

        do_POST = handle_request
        do_GET = handle_request

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, *_):
            pass

        def server_close(self):
            super().server_close()
            jobs.close()

    try:
        server = Server((listen, port), Handler)
        server.jobs = jobs
        return server
    except BaseException:
        jobs.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--targets-file', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--listen', choices=('127.0.0.1',), default='127.0.0.1')
    parser.add_argument('--port', type=int, default=4180)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be 1..65535')
    def drain(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, drain)
    try:
        with create_server(**vars(args)) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('Ansible API configuration or private state is unavailable.', file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
