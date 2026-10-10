#!/usr/bin/env python3
"""Single-writer Cloudflare DNS registration; API readback is not DNS propagation.

Operator config/token/state are private, owned local files. A durable create intent
is never automatically retried, including if the process died before sending it.
API contracts: https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/list/
and https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/create/
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci/scripts'))
from storage import durable_write

API = 'https://api.cloudflare.com/client/v4/zones/'
LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
IDENTIFIER = r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}'
MAX_RESPONSE = 1_000_000
PAGE_SIZE = 100
MAX_PAGES = 20


class DNSError(RuntimeError):
    """Only static codes cross this boundary; provider messages and tokens do not."""
    def __init__(self, code, outcome='BLOCKED'):
        self.code, self.outcome = code, outcome
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise DNSError(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def private_bytes(path, maximum):
    """NOFOLLOW plus fstat binds the privacy check to the opened file."""
    try:
        path = Path(path)
        require(path.is_absolute(), 'DNS_PRIVATE_FILE_REQUIRED')
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                    not info.st_mode & 0o077 and info.st_size <= maximum, 'DNS_PRIVATE_FILE_REQUIRED')
            data = stream.read(maximum + 1)
        require(len(data) <= maximum, 'DNS_PRIVATE_FILE_REQUIRED')
        return data
    except DNSError:
        raise
    except (OSError, TypeError, ValueError):
        raise DNSError('DNS_PRIVATE_FILE_REQUIRED') from None


def private_json(path):
    try:
        return json.loads(private_bytes(path, MAX_RESPONSE))
    except (ValueError, UnicodeError):
        raise DNSError('DNS_PRIVATE_JSON_INVALID') from None


def fqdn(value):
    return (isinstance(value, str) and 3 <= len(value) <= 253 and '.' in value and
            all(re.fullmatch(LABEL, part) for part in value.split('.')))


def config_at(path):
    config = private_json(path)
    require(isinstance(config, dict) and set(config) == {
        'version', 'zone_id', 'base_domain', 'token_file', 'state_dir'} and
        type(config['version']) is int and config['version'] == 1, 'DNS_CONFIG_INVALID')
    require(isinstance(config['zone_id'], str) and re.fullmatch(r'[0-9a-f]{32}', config['zone_id']) and
            fqdn(config['base_domain']), 'DNS_CONFIG_INVALID')
    require(all(isinstance(config[key], str) and Path(config[key]).is_absolute()
                for key in ('token_file', 'state_dir')), 'DNS_CONFIG_INVALID')
    config['_sha256'] = digest(config)
    return config


def request_at(config, value):
    required = {'application_id', 'hostname', 'type', 'content'}
    require(isinstance(value, dict) and required <= set(value) <= required | {
        'purpose', 'application_hostname', 'proxied'}, 'DNS_REQUEST_INVALID')
    row = dict(value)
    row.setdefault('purpose', 'application')
    require(isinstance(row['application_id'], str) and re.fullmatch(IDENTIFIER, row['application_id']) and
            row['purpose'] in ('application', 'certificate') and row['type'] in ('A', 'CNAME'),
            'DNS_REQUEST_INVALID')
    require(type(row.get('proxied', False)) is bool, 'DNS_PROXY_INVALID')
    if row.get('proxied', False):
        require(row['purpose'] == 'application' and row['type'] == 'CNAME' and
                isinstance(row['content'], str) and re.fullmatch(
                    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.cfargotunnel\.com',
                    row['content']), 'DNS_PROXY_INVALID')
    # Keep absent proxied absent: existing durable request bindings predate this option.
    app_host = row.get('application_hostname', row['hostname'])
    child = LABEL + r'\.' + re.escape(config['base_domain'])
    require(isinstance(app_host, str) and re.fullmatch(child, app_host) and len(app_host) <= 253,
            'DNS_HOSTNAME_OUTSIDE_APPLICATION')
    if row['purpose'] == 'application':
        require(row['hostname'] == app_host, 'DNS_HOSTNAME_OUTSIDE_APPLICATION')
    else:
        require('application_hostname' in row and row['type'] == 'CNAME' and
                isinstance(row['hostname'], str) and len(row['hostname']) <= 253 and
                re.fullmatch(r'_acme-challenge(?:_[a-z0-9]+)?\.' + re.escape(app_host), row['hostname']),
                'DNS_CERTIFICATE_HOSTNAME_INVALID')
        require(len(row['hostname'].split('.')[0]) <= 63, 'DNS_CERTIFICATE_HOSTNAME_INVALID')
    row['application_hostname'] = app_host
    if row['type'] == 'A':
        try:
            require(isinstance(row['content'], str) and str(ipaddress.IPv4Address(row['content'])) == row['content'],
                    'DNS_CONTENT_INVALID')
        except ipaddress.AddressValueError:
            raise DNSError('DNS_CONTENT_INVALID') from None
    else:
        require(fqdn(row['content']) and row['content'] != row['hostname'], 'DNS_CONTENT_INVALID')
    return row


@contextmanager
def locked(config):
    root = Path(config['state_dir'])
    try:
        require(not root.is_symlink(), 'DNS_PRIVATE_STATE_REQUIRED')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                'DNS_PRIVATE_STATE_REQUIRED')
        with os.fdopen(os.open(root / 'dns.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                               0o600), 'a') as lock:
            info = os.fstat(lock.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                    'DNS_PRIVATE_STATE_REQUIRED')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DNSError('DNS_WRITER_BUSY') from None
            yield root
    except DNSError:
        raise
    except OSError:
        raise DNSError('DNS_STATE_IO_FAILED') from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def transport(config, method, *, query=None, body=None, record_id=None):
    """Fixed-origin, bounded HTTPS only. No redirects, proxies, logging or retries."""
    require(method in ('GET', 'POST', 'DELETE'), 'DNS_METHOD_INVALID')
    require((method == 'DELETE' and isinstance(record_id, str) and re.fullmatch(r'[0-9a-f]{32}', record_id)
             and query is None and body is None) or (method != 'DELETE' and record_id is None), 'DNS_RECORD_ID_INVALID')
    token = private_bytes(config['token_file'], 4096)
    try:
        token = token.decode('ascii').rstrip('\n')
        require(token and all(33 <= ord(char) <= 126 for char in token), 'DNS_TOKEN_INVALID')
        url = API + config['zone_id'] + '/dns_records'
        if record_id is not None:
            url += '/' + record_id
        if query:
            url += '?' + urlencode(query)
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
                   'Content-Type': 'application/json'}
        request = Request(url, data=None if body is None else encoded(body), headers=headers, method=method)
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
            require(200 <= response.status < 300, 'DNS_API_FAILED')
            raw = response.read(MAX_RESPONSE + 1)
        require(len(raw) <= MAX_RESPONSE, 'DNS_API_RESPONSE_INVALID')
        data = json.loads(raw)
        require(isinstance(data, dict) and data.get('success') is True and not data.get('errors'),
                'DNS_API_RESPONSE_INVALID')
        return data
    except DNSError:
        raise
    except Exception:
        # Never preserve an HTTPError, request headers or provider body in a public cause chain.
        raise DNSError('DNS_API_FAILED') from None


def records_at(config, hostname):
    records, expected_count = [], None
    for page in range(1, MAX_PAGES + 1):
        data = transport(config, 'GET', query={'name.exact': hostname, 'page': page, 'per_page': PAGE_SIZE})
        values, info = data.get('result'), data.get('result_info')
        require(isinstance(values, list) and isinstance(info, dict) and
                all(type(info.get(key)) is int for key in ('page', 'per_page', 'count', 'total_count', 'total_pages')) and
                info['page'] == page and info['per_page'] == PAGE_SIZE and info['count'] == len(values) and
                0 <= len(values) <= PAGE_SIZE and 0 <= info['total_count'] <= PAGE_SIZE * MAX_PAGES and
                info['total_pages'] in ({0, 1} if info['total_count'] == 0 else
                                       {(info['total_count'] + PAGE_SIZE - 1) // PAGE_SIZE}),
                'DNS_LIST_INCOMPLETE')
        require(expected_count is None or expected_count == info['total_count'], 'DNS_LIST_CHANGED')
        expected_count = info['total_count']
        require(all(isinstance(record, dict) and record.get('name') == hostname for record in values),
                'DNS_LIST_RESPONSE_INVALID')
        records.extend(values)
        if page >= info['total_pages']:
            require(len(records) == expected_count, 'DNS_LIST_INCOMPLETE')
            return records
    raise DNSError('DNS_LIST_INCOMPLETE')


def owned_record(records, request):
    if not records:
        return None
    require(len(records) == 1, 'DNS_RECORD_CONFLICT')
    record = records[0]
    proxied = request.get('proxied', False)
    require(record.get('comment') == 'railshot:' + request['application_id'], 'DNS_RECORD_FOREIGN_OWNER')
    require(record.get('type') == request['type'] and record.get('content') == request['content'] and
            record.get('proxied') is proxied and type(record.get('ttl')) is int and
            record['ttl'] == (1 if proxied else 300),
            'DNS_RECORD_CONFLICT')
    require(isinstance(record.get('id'), str) and re.fullmatch(r'[0-9a-f]{32}', record['id']),
            'DNS_RECORD_RESPONSE_INVALID')
    return record


def save(path, value):
    try:
        durable_write(path, encoded(value))
    except (OSError, ValueError):
        raise DNSError('DNS_STATE_IO_FAILED', 'UNKNOWN') from None


def application_snapshot(config, application_id, hostname):
    """Caller holds dns.lock. Bind all app/certificate records to durable ownership."""
    result = []
    for path in sorted(Path(config['state_dir']).glob('*.json')):
        if not re.fullmatch(r'[a-f0-9]{64}\.json', path.name):
            continue
        row = private_json(path)
        request = row.get('request', {})
        if request.get('application_id') != application_id:
            continue
        request_at(config, request)
        require(row.get('config_sha256') == config['_sha256'] and
                request.get('application_hostname', request['hostname']) == hostname and
                path.stem == digest({'zone_id': config['zone_id'], 'hostname': request['hostname']}),
                'DNS_APPLICATION_BINDING_CHANGED')
        require(row.get('phase') in ('verified', 'deleted'), 'DNS_APPLICATION_RECONCILE_REQUIRED')
        found = owned_record(records_at(config, request['hostname']), request)
        if row['phase'] == 'deleted':
            require(found is None, 'DNS_DELETED_RECORD_REAPPEARED')
            continue
        require(found is not None and found['id'] == row.get('receipt', {}).get('record_id'),
                'DNS_APPLICATION_RECORD_CHANGED')
        result.append({'path': str(path), 'row': row, 'record': found})
    if not any(item['row']['request']['hostname'] == hostname for item in result):
        require(not records_at(config, hostname), 'DNS_APPLICATION_UNRECORDED')
    return result


def remove_application(config, application_id, hostname, expected):
    """Delete exact record IDs once, under the existing DNS lock; verify absence."""
    require(application_snapshot(config, application_id, hostname) == expected, 'DNS_APPLICATION_PLAN_STALE')
    for item in expected:
        path, row = item['path'], item['row']
        # A transport/storage failure leaves deleting, which no writer can replay.
        save(path, {**row, 'phase': 'deleting'})
        transport(config, 'DELETE', record_id=item['record']['id'])
        require(not records_at(config, row['request']['hostname']), 'DNS_DELETE_UNVERIFIED')
        save(path, {**row, 'phase': 'deleted'})
    require(not application_snapshot(config, application_id, hostname), 'DNS_DELETE_UNVERIFIED')


def ensure(config_path, request):
    """Ensure an owned record. Existing intents reconcile read-only, never POST again.

    Returns a private receipt with status=verified on exact API readback. It makes
    no claim about delegation, propagation, TLS, reachability or application health.
    """
    config = config_at(config_path)
    request = request_at(config, request)
    key = digest({'zone_id': config['zone_id'], 'hostname': request['hostname']})
    with locked(config) as root:
        path = root / (key + '.json')
        previous = private_json(path) if path.exists() or path.is_symlink() else None
        mutation_pending = isinstance(previous, dict) and previous.get('phase') == 'creating'
        try:
            if previous is not None:
                require(isinstance(previous, dict) and previous.get('version') == 1 and
                        previous.get('config_sha256') == config['_sha256'] and previous.get('request') == request and
                        previous.get('phase') in ('creating', 'verified'), 'DNS_INTENT_BINDING_CONFLICT')
            found = owned_record(records_at(config, request['hostname']), request)
            if found is None:
                if previous is not None:
                    raise DNSError('DNS_CREATE_OUTCOME_UNKNOWN' if previous['phase'] == 'creating'
                                   else 'DNS_VERIFIED_RECORD_MISSING',
                                   'UNKNOWN' if previous['phase'] == 'creating' else 'BLOCKED')
                intent = {'version': 1, 'phase': 'creating', 'request': request,
                          'config_sha256': config['_sha256'], 'intent_at': now()}
                save(path, intent)  # Commit before POST; a crash at either side is read-only on resume.
                mutation_pending = True
                proxied = request.get('proxied', False)
                payload = {'name': request['hostname'], 'type': request['type'], 'content': request['content'],
                           'comment': 'railshot:' + request['application_id'],
                           'proxied': proxied, 'ttl': 1 if proxied else 300}
                try:
                    transport(config, 'POST', body=payload)
                except DNSError:
                    pass  # Even a rejected/timed-out write is reconciled, never automatically retried.
                try:
                    found = owned_record(records_at(config, request['hostname']), request)
                except DNSError as error:
                    raise DNSError(error.code, 'UNKNOWN') from None
                if found is None:
                    raise DNSError('DNS_CREATE_OUTCOME_UNKNOWN', 'UNKNOWN')
            receipt = {'version': 1, 'status': 'verified', **request, 'zone_id': config['zone_id'],
                       'record_id': found['id'], 'verified_at': now(), 'receipt_path': str(path)}
            row = {'version': 1, 'phase': 'verified', 'request': request,
                   'config_sha256': config['_sha256'], 'receipt': receipt}
            save(path, row)
            return receipt
        except DNSError as error:
            if mutation_pending and error.outcome != 'UNKNOWN':
                error = DNSError(error.code, 'UNKNOWN')
            save(root / (key + '.error.json'), {'version': 1, 'request': request, 'observed_at': now(),
                 'error': {'code': error.code, 'outcome': error.outcome}})
            raise error from None
