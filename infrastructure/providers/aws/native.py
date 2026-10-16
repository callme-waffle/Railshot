"""Private native command boundary and renewable account-bound AWS sessions."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile


class AwsError(RuntimeError):
    def __init__(self, code, *, unknown=False):
        self.code, self.outcome_unknown = code, unknown
        self.status = 409
        super().__init__(code)


def private_json(path):
    path = Path(path)
    info = path.lstat()
    if not path.is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4 * 1024 * 1024:
        raise AwsError('AWS_PRIVATE_FILE_INVALID')
    return json.loads(path.read_text())


def directory(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise AwsError('AWS_STATE_PATH_INVALID')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or path.resolve() != path:
        raise AwsError('AWS_STATE_PATH_INVALID')
    return path


def save(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(name): os.unlink(name)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def command(argv, *, env, cwd=None, timeout=120, mutation=False):
    """Never expose CLI stderr (which may contain credentials); reap the whole group."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, start_new_session=True, umask=0o077)
        except OSError as error:
            raise AwsError('AWS_EXECUTOR_UNAVAILABLE') from error
        try:
            try: code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise AwsError('AWS_EXECUTOR_TIMEOUT', unknown=mutation) from error
            if code:
                raise AwsError('AWS_NATIVE_COMMAND_FAILED', unknown=mutation)
            out.seek(0); data = out.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise AwsError('AWS_RESPONSE_TOO_LARGE', unknown=mutation)
            return data.decode()
        finally:
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            child.wait()


class Session:
    """Explicit source credentials or an explicitly selected controller workload role.

    Source secrets are read afresh on every renewal. Customer mode always assumes
    the registered customer role with its ExternalId. A session is never persisted
    as the durable environment identity.
    """
    def __init__(self, config, account, *, runner=command, now=None):
        if not isinstance(config, dict) or set(config) - {'mode', 'role_arn', 'external_id', 'source_credentials_file', 'source'}:
            raise AwsError('AWS_AUTH_CONFIGURATION_INVALID')
        self.config, self.account, self.runner = config, account, runner
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.cached, self.expires = None, None
        if not re.fullmatch(r'\d{12}', account): raise AwsError('AWS_ACCOUNT_INVALID')
        if config.get('mode') not in ('customer_role', 'managed_role'):
            raise AwsError('AWS_AUTH_MODE_INVALID')
        role = config.get('role_arn', '')
        if not re.fullmatch(r'arn:aws:iam::' + account + r':role/[A-Za-z0-9_+=,.@/-]+', role):
            raise AwsError('AWS_ROLE_ACCOUNT_MISMATCH')
        if config['mode'] == 'customer_role' and not re.fullmatch(r'[A-Za-z0-9_+=,.@:/-]{2,1224}', config.get('external_id', '')):
            raise AwsError('AWS_EXTERNAL_ID_REQUIRED')
        if ('source_credentials_file' in config) == (config.get('source') == 'instance_profile'):
            raise AwsError('AWS_AUTH_SOURCE_INVALID')

    def base(self):
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'TF_PLUGIN_CACHE_DIR') if k in os.environ}
        env.update(AWS_REGION='ap-northeast-2', AWS_DEFAULT_REGION='ap-northeast-2', AWS_PAGER='',
            AWS_CONFIG_FILE='/dev/null', AWS_SHARED_CREDENTIALS_FILE='/dev/null', AWS_EC2_METADATA_DISABLED='true',
            TF_IN_AUTOMATION='1', TF_INPUT='0')
        if self.config.get('source') == 'instance_profile':
            env['AWS_EC2_METADATA_DISABLED'] = 'false'
        else:
            creds = private_json(self.config['source_credentials_file'])
            required = {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'}
            if not required <= creds.keys() or set(creds) - required - {'AWS_SESSION_TOKEN'} or any(not isinstance(v, str) or not v or '\0' in v for v in creds.values()):
                raise AwsError('AWS_CREDENTIAL_FILE_INVALID')
            env.update(creds)
        return env

    def env(self):
        if self.cached is None or self.expires <= self.now() + dt.timedelta(minutes=35):
            base = self.base()
            argv = ['aws', 'sts', 'assume-role', '--role-arn', self.config['role_arn'],
                '--role-session-name', 'railshot-environment', '--duration-seconds', '3600', '--output', 'json']
            if self.config['mode'] == 'customer_role': argv += ['--external-id', self.config['external_id']]
            try:
                result = json.loads(self.runner(argv, env=base))['Credentials']
                expires = dt.datetime.fromisoformat(result['Expiration'].replace('Z', '+00:00'))
                env = {k: v for k, v in base.items() if not k.startswith('AWS_ACCESS') and k not in ('AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')}
                env.update(AWS_ACCESS_KEY_ID=result['AccessKeyId'], AWS_SECRET_ACCESS_KEY=result['SecretAccessKey'],
                    AWS_SESSION_TOKEN=result['SessionToken'], AWS_EC2_METADATA_DISABLED='true')
                identity = json.loads(self.runner(['aws', 'sts', 'get-caller-identity', '--output', 'json'], env=env))
                if identity.get('Account') != self.account or expires <= self.now() + dt.timedelta(minutes=35):
                    raise AwsError('AWS_CREDENTIAL_SCOPE_INVALID')
                self.cached, self.expires = env, expires
            except (KeyError, ValueError, TypeError) as error:
                raise AwsError('AWS_CREDENTIAL_RESPONSE_INVALID') from error
        return dict(self.cached)

    def call(self, service, action, *args, mutation=False):
        raw = self.runner(['aws', service, action, *args, '--region', 'ap-northeast-2', '--output', 'json'],
            env=self.env(), mutation=mutation)
        try: return json.loads(raw or '{}')
        except ValueError as error: raise AwsError('AWS_RESPONSE_INVALID', unknown=mutation) from error

    def materialize(self, path, *, minimum_seconds=2100):
        if self.expires is not None and self.expires <= self.now() + dt.timedelta(seconds=minimum_seconds):
            self.cached = None
        env = self.env()
        if self.expires <= self.now() + dt.timedelta(seconds=minimum_seconds):
            raise AwsError('AWS_SESSION_TOO_SHORT')
        save(path, {k: env[k] for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')})
