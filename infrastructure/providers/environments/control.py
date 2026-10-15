"""Durable owner-scoped orchestration around real provider adapters."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

from .contract import ProviderError, PROVIDERS, READINESS, identifier, ready, validate_spec


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def private_dir(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ProviderError('PRIVATE_STATE_INVALID', 503)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or path.resolve() != path:
        raise ProviderError('PRIVATE_STATE_INVALID', 503)
    return path


def read_private(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ProviderError('PRIVATE_CONFIGURATION_INVALID', 503)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 4 * 1024 * 1024):
            raise ProviderError('PRIVATE_CONFIGURATION_INVALID', 503)
        return json.loads(stream.read(4 * 1024 * 1024 + 1))


def save(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProviderError('PRIVATE_STATE_INVALID', 503)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProviderError('ENVIRONMENT_BUSY', 409) from None
        yield
    finally:
        os.close(fd)


def factory(provider, config):
    module = 'onprem' if provider == 'openstack' else provider
    try:
        selected = importlib.import_module('.' + module, __package__)
    except ModuleNotFoundError as exc:
        if exc.name == __package__ + '.' + module:
            raise ProviderError('PROVIDER_UNAVAILABLE', 503) from None
        raise ProviderError('PROVIDER_DEPENDENCY_UNAVAILABLE', 503) from None
    return selected.Adapter(config)


def public(record):
    output = {key: record[key] for key in ('id', 'spec', 'status', 'created_at', 'updated_at')}
    result = record.get('result', {})
    for key in ('created_at', 'updated_at'):
        output[key] = datetime.fromtimestamp(output[key], timezone.utc).isoformat().replace('+00:00', 'Z')
    output['readiness'] = {key: result.get('readiness', {}).get(key) is True for key in READINESS}
    output['deployable'] = record['status'] == 'ready' and ready(result, record['spec'])
    output['resources'] = [{key: item[key] for key in ('kind', 'id', 'status') if key in item}
                           for item in result.get('resources', []) if isinstance(item, dict)]
    output['retained'] = [{key: item[key] for key in ('kind', 'id', 'status') if key in item}
                          for item in result.get('retained', []) if isinstance(item, dict)]
    output['operations'] = [{key: operation[key] for key in ('id', 'action', 'status', 'created_at', 'updated_at', 'error') if key in operation}
                            for operation in record.get('operations', {}).values()]
    for operation in output['operations']:
        for key in ('created_at', 'updated_at'):
            operation[key] = datetime.fromtimestamp(operation[key], timezone.utc).isoformat().replace('+00:00', 'Z')
    plan = record.get('plan', {})
    output['plan'] = {'hash': record.get('plan_hash'), 'expires_at': datetime.fromtimestamp(record['plan_expires_at'], timezone.utc).isoformat().replace('+00:00', 'Z') if record.get('plan_expires_at') else None,
                      'changes': [{'kind': item.get('kind'), 'action': item.get('action'), 'count': item.get('count')}
                                  for item in plan.get('changes', []) if isinstance(item, dict)
                                  and item.get('action') in ('create', 'update', 'retain', 'delete')
                                  and isinstance(item.get('kind'), str) and len(item['kind']) < 64
                                  and type(item.get('count')) is int and 0 <= item['count'] <= 1000],
                      'retention': 'Provider deletion retains persistent data unless a separate approved data operation removes it.'}
    output['blockers'] = [code for code in result.get('blockers', [])
                          if isinstance(code, str) and code.isupper() and len(code) < 96]
    return output


class ControlPlane:
    def __init__(self, config_path, adapter_factory=factory, clock=time.time):
        self.config_path, self.adapter_factory, self.clock = config_path, adapter_factory, clock
        self.config = read_private(config_path)
        if (self.config.get('version') != 1 or not isinstance(self.config.get('accounts'), list)
                or not identifier(self.config.get('policy_revision'))):
            raise ProviderError('REGISTRY_INVALID', 503)
        self.root = private_dir(self.config['state_dir'])
        seen = set()
        for account in self.config['accounts']:
            if (not isinstance(account, dict) or not identifier(account.get('id')) or account['id'] in seen
                    or account.get('provider') not in PROVIDERS or account.get('ownership') not in ('owned', 'managed')
                    or not identifier(account.get('owner_id')) or not identifier(account.get('credential_ref'))
                    or not isinstance(account.get('adapter'), dict) or not isinstance(account.get('regions'), list)
                    or not account['regions'] or not all(identifier(region) for region in account['regions'])
                    or not isinstance(account.get('allow_apply'), bool)):
                raise ProviderError('REGISTRY_INVALID', 503)
            seen.add(account['id'])

    def account(self, account_id, owner):
        account = next((item for item in self.config['accounts'] if item['id'] == account_id and item['owner_id'] == owner), None)
        if not account:
            raise ProviderError('ACCOUNT_NOT_FOUND', 404)
        return account

    def binding(self, account):
        # Credentials rotate behind a stable reference; never hash temporary session bytes.
        return digest({'account': account, 'policy_revision': self.config['policy_revision']})

    def context(self, record, account, operation_id=None):
        return {'environment_id': record['id'], 'owner_id': record['owner_id'], 'account_id': account['id'],
                'credential_ref': account['credential_ref'], 'policy_revision': self.config['policy_revision'],
                'state_dir': str(private_dir(self.root / record['id'])), 'operation_id': operation_id,
                'spec': record['spec']}

    def load(self, environment_id, owner):
        if not identifier(environment_id) or '/' in environment_id:
            raise ProviderError('ENVIRONMENT_NOT_FOUND', 404)
        try:
            record = read_private(self.root / (environment_id + '.json'))
        except FileNotFoundError:
            raise ProviderError('ENVIRONMENT_NOT_FOUND', 404) from None
        if record['owner_id'] != owner:
            raise ProviderError('ENVIRONMENT_NOT_FOUND', 404)
        return record

    def persist(self, record):
        record['updated_at'] = self.clock()
        save(self.root / (record['id'] + '.json'), record)

    def dispatch(self, request):
        if not isinstance(request, dict) or not identifier(request.get('owner_id')):
            raise ProviderError('AUTHENTICATION_REQUIRED', 401)
        action, owner = request.get('action'), request['owner_id']
        if action == 'accounts':
            return {'items': [{key: a[key] for key in ('id', 'provider', 'ownership', 'regions', 'allow_apply')}
                              for a in self.config['accounts'] if a['owner_id'] == owner]}
        if action == 'list':
            return {'items': [public(row) for row in (read_private(path) for path in sorted(self.root.glob('*.json')))
                              if row['owner_id'] == owner]}
        if action in ('connection', 'catalog'):
            account = self.account(request.get('account_id'), owner)
            adapter = self.adapter_factory(account['provider'], account['adapter'])
            record = {'id': 'account-' + digest([owner, account['id']])[:24], 'owner_id': owner, 'spec': None}
            return getattr(adapter, action)(self.context(record, account))
        if action == 'plan':
            spec = validate_spec(request.get('spec'))
            account = self.account(spec['account_id'], owner)
            if account['provider'] != spec['provider'] or spec['region'] not in account['regions']:
                raise ProviderError('ACCOUNT_SCOPE_MISMATCH', 403)
            key = request.get('idempotency_key')
            if not identifier(key):
                raise ProviderError('IDEMPOTENCY_KEY_REQUIRED', 422)
            environment_id = 'env-' + digest([owner, key])[:24]
            with locked(self.root / (environment_id + '.lock')):
                try:
                    record = self.load(environment_id, owner)
                    if record['spec'] != spec:
                        raise ProviderError('IDEMPOTENCY_CONFLICT', 409)
                    return public(record)
                except ProviderError as exc:
                    if exc.code != 'ENVIRONMENT_NOT_FOUND':
                        raise
                record = {'id': environment_id, 'owner_id': owner, 'spec': spec, 'status': 'planned',
                          'created_at': self.clock(), 'updated_at': self.clock(), 'binding': self.binding(account),
                          'operations': {}, 'result': {}}
                adapter = self.adapter_factory(account['provider'], account['adapter'])
                record['plan'] = adapter.plan(spec, self.context(record, account))
                record['plan_hash'] = digest({'spec': spec, 'binding': record['binding'], 'plan': record['plan']})
                record['plan_expires_at'] = self.clock() + 1200
                self.persist(record)
                return public(record)
        if action not in ('get', 'apply', 'describe', 'reconcile', 'operate', 'delete', 'binding', 'confirm_binding', 'admit'):
            raise ProviderError('ACTION_UNSUPPORTED', 422)
        record = self.load(request.get('environment_id'), owner)
        account = self.account(record['spec']['account_id'], owner)
        if action == 'get':
            return public(record)
        with locked(self.root / (record['id'] + '.lock')):
            record = self.load(record['id'], owner)
            if record['binding'] != self.binding(account):
                raise ProviderError('ACCOUNT_POLICY_CHANGED', 409)
            if action in ('binding', 'confirm_binding'):
                result = record.get('result', {})
                evidence = result.get('readiness', {})
                required = ('infrastructure', 'cluster', 'storage') + (('database',) if record['spec']['database']['mode'] == 'standalone' else ())
                if result.get('status') != 'succeeded' or not all(evidence.get(key) is True for key in required):
                    raise ProviderError('ENVIRONMENT_NOT_READY', 409)
                path = result.get('application_config_path')
                if not isinstance(path, str) or not Path(path).is_absolute():
                    raise ProviderError('APPLICATION_BINDING_MISSING', 409)
                binding_hash = digest(read_private(path))
                if action == 'confirm_binding':
                    if request.get('binding_hash') != binding_hash:
                        raise ProviderError('APPLICATION_BINDING_CHANGED', 409)
                    result.setdefault('readiness', {})['deployment_binding'] = True
                    record['status'] = 'ready'
                    for operation in record['operations'].values():
                        if operation['action'] == 'apply' and operation['status'] == 'blocked':
                            operation['status'] = 'succeeded'
                    self.persist(record)
                    return public(record)
                return {'environment_id': record['id'], 'spec': record['spec'], 'application_config_path': path, 'binding_hash': binding_hash}
            adapter = self.adapter_factory(account['provider'], account['adapter'])
            if action in ('describe', 'reconcile'):
                result = adapter.describe(self.context(record, account))
                self.validate_result(result)
                record['result'] = result
                record['status'] = 'ready' if ready(result, record['spec']) else 'unknown'
                if result.get('status') == 'succeeded' and result.get('state') == 'stopped' and result.get('stopped_verified') is True:
                    record['status'] = 'stopped'
                if result.get('status') == 'succeeded' and result.get('deleted') is True and result.get('residuals') == []:
                    record['status'] = 'deleted'
                # Native observations clear uncertainty only when the requested end state is proven.
                for operation in record['operations'].values():
                    if operation['status'] in ('running', 'unknown'):
                        if (operation['action'] == 'apply' and record['status'] == 'ready'
                                or operation['action'] == 'delete' and record['status'] == 'deleted'
                                or operation['action'] == 'operate' and operation.get('operation', {}).get('action') == 'stop' and record['status'] == 'stopped'
                                or operation['action'] == 'operate' and operation.get('operation', {}).get('action') == 'start' and record['status'] == 'ready'):
                            operation['status'] = 'succeeded'
                        else:
                            operation['status'] = 'unknown'
                        operation['updated_at'] = self.clock()
                self.persist(record)
                return public(record)
            admission = action == 'admit'
            if admission:
                action = request.get('operation_action')
                if action not in ('apply', 'operate', 'delete'):
                    raise ProviderError('ACTION_UNSUPPORTED', 422)
            if action == 'operate':
                payload = request.get('operation')
                if not isinstance(payload, dict) or payload.get('action') not in ('start', 'stop', 'backup', 'restore', 'verify'):
                    raise ProviderError('OPERATION_UNSUPPORTED', 422)
            if not account['allow_apply']:
                raise ProviderError('ACCOUNT_EXECUTION_DISABLED', 403)
            key = request.get('idempotency_key')
            if not identifier(key):
                raise ProviderError('IDEMPOTENCY_KEY_REQUIRED', 422)
            operation_id = digest([owner, record['id'], key])[:32]
            fingerprint = digest({'action': action, 'operation': request.get('operation')})
            existing = record['operations'].get(operation_id)
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise ProviderError('IDEMPOTENCY_CONFLICT', 409)
                if existing['status'] == 'queued' and not admission:
                    operation = existing
                else:
                    if existing['status'] == 'running' and not admission:
                        existing['status'] = record['status'] = 'unknown'
                        self.persist(record)
                    return public(record)
            else:
                if record['status'] in ('queued', 'running', 'unknown'):
                    raise ProviderError('RECONCILIATION_REQUIRED', 409, True)
                if action == 'apply' and record['status'] != 'planned':
                    raise ProviderError('ENVIRONMENT_ALREADY_APPLIED', 409)
                if action == 'apply' and record.get('plan_expires_at', 0) <= self.clock():
                    raise ProviderError('ENVIRONMENT_PLAN_EXPIRED', 409)
                if action == 'operate' and record['status'] != 'ready' and not (record['status'] == 'stopped' and request.get('operation', {}).get('action') == 'start'):
                    raise ProviderError('ENVIRONMENT_NOT_READY', 409)
                if record['status'] == 'deleted':
                    raise ProviderError('ENVIRONMENT_DELETED', 409)
                operation = {'id': operation_id, 'action': action, 'fingerprint': fingerprint,
                             'status': 'queued', 'created_at': self.clock(), 'updated_at': self.clock(),
                             'operation': request.get('operation')}
                record['operations'][operation_id] = operation
                record['status'] = 'queued'
                self.persist(record)
            if admission:
                return public(record)
            operation['status'] = record['status'] = 'running'
            self.persist(record)  # Dispatch marker is durable before any native side effect.
            context = self.context(record, account, operation_id)
            try:
                if action == 'apply':
                    result = adapter.apply(record['spec'], context)
                elif action == 'delete':
                    result = adapter.delete(context)
                else:
                    result = adapter.operate(request.get('operation'), context)
                self.validate_result(result)
                record['result'] = result
                if result['status'] == 'unknown':
                    record['status'] = operation['status'] = 'unknown'
                elif result['status'] != 'succeeded':
                    operation['status'] = 'blocked'
                    record['status'] = 'planned' if action == 'apply' and result.get('side_effect') == 'none' else 'blocked'
                elif action == 'delete':
                    if result.get('deleted') is not True or result.get('residuals') != []:
                        raise ProviderError('DELETION_UNVERIFIED', 502, True)
                    record['status'], operation['status'] = 'deleted', 'succeeded'
                elif action == 'operate' and request.get('operation', {}).get('action') == 'stop':
                    if result.get('state') != 'stopped' or result.get('stopped_verified') is not True:
                        raise ProviderError('STOP_UNVERIFIED', 502, True)
                    record['status'], operation['status'] = 'stopped', 'succeeded'
                else:
                    record['status'] = 'ready' if ready(result, record['spec']) else 'blocked'
                    operation['status'] = 'succeeded' if record['status'] == 'ready' else 'blocked'
            except Exception as exc:
                # Even provider failure may occur after resource creation; reconcile before retry.
                record['status'] = operation['status'] = 'unknown'
                operation['error'] = exc.code if isinstance(exc, ProviderError) else 'PROVIDER_OUTCOME_UNKNOWN'
            operation['updated_at'] = self.clock()
            self.persist(record)
            return public(record)

    @staticmethod
    def validate_result(result):
        if not isinstance(result, dict) or result.get('status') not in ('succeeded', 'blocked', 'unknown'):
            raise ProviderError('PROVIDER_RESPONSE_INVALID', 502, True)
        if not isinstance(result.get('readiness', {}), dict) or not isinstance(result.get('resources', []), list):
            raise ProviderError('PROVIDER_RESPONSE_INVALID', 502, True)
