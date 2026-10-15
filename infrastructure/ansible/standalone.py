"""Owned standalone PostgreSQL runner. No caller-selected command or playbook."""
from contextlib import ExitStack
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
import time

import run as ansible
from database import private_bytes

HERE = Path(__file__).resolve().parent
ACTIONS = {'install', 'status', 'verify', 'backup', 'restore', 'bind'}
BACKUP_ID = r'[0-9]{8}-[0-9]{6}F'


class StandaloneError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read(path):
    return json.loads(private_bytes(str(path)))


def save(path, value):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, sort_keys=True); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally: temporary.unlink(missing_ok=True)


def private_dir(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path: raise StandaloneError('DATABASE_PRIVATE_STATE_INVALID')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise StandaloneError('DATABASE_PRIVATE_STATE_INVALID')
    return path


def action_spec(action):
    value = {'action': action} if isinstance(action, str) else action
    if not isinstance(value, dict) or value.get('action') not in ACTIONS:
        raise StandaloneError('DATABASE_ACTION_UNSUPPORTED')
    expected = {'action'} | ({'backup_id'} if value['action'] == 'restore' else {'binding_file'} if value['action'] == 'bind' else set())
    if set(value) != expected: raise StandaloneError('DATABASE_ACTION_INVALID')
    if value['action'] == 'restore' and not re.fullmatch(BACKUP_ID, value['backup_id']):
        raise StandaloneError('DATABASE_BACKUP_ID_INVALID')
    return value


def validate_config(config):
    required = {'version', 'client_cidrs', 'tls', 'probe_password_file', 'backup'}
    missing = [k for k in required if not isinstance(config, dict) or not config.get(k)]
    if missing: return {'valid': False, 'missing': sorted(missing), 'errors': []}
    try:
        if set(config) - required - {'ssh', 'data_device', 'restore_approval_file', 'timeout_seconds'} or type(config['version']) is not int or config['version'] != 1: raise ValueError()
        if not isinstance(config['client_cidrs'], list) or not 1 <= len(config['client_cidrs']) <= 32: raise ValueError()
        for cidr in config['client_cidrs']:
            network = ipaddress.ip_network(cidr, strict=True)
            if network.version != 4 or not any(network.subnet_of(p) for p in ansible.PRIVATE): raise ValueError()
        tls = config['tls']
        if set(tls) != {'ca_file', 'certificate_file', 'key_file', 'server_name'} or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', tls['server_name']): raise ValueError()
        for name in ('ca_file', 'certificate_file', 'key_file'): private_bytes(tls[name])
        password = private_bytes(config['probe_password_file']).decode().strip()
        if len(password) < 24 or any(c in password for c in ('\n', '\r', '\x00')): raise ValueError()
        backup = config['backup']
        base = {'type', 'bucket', 'prefix', 'retention_full', 'identity'}
        if backup.get('type') == 's3':
            if not base | {'endpoint', 'region'} <= set(backup) or set(backup) - base - {'endpoint', 'region', 'credentials_file'}: raise ValueError()
            if backup['identity'] not in ('instance', 'secret') or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', backup['endpoint']) or not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', backup['region']): raise ValueError()
        elif backup.get('type') == 'gcs':
            if not base <= set(backup) or set(backup) - base - {'credentials_file'} or backup['identity'] not in ('instance', 'service'): raise ValueError()
        else: raise ValueError()
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{2,221}', backup['bucket']) or not re.fullmatch(r'/[a-zA-Z0-9/_-]{1,180}', backup['prefix']) or '..' in backup['prefix']: raise ValueError()
        if type(backup['retention_full']) is not int or not 2 <= backup['retention_full'] <= 30: raise ValueError()
        if backup['identity'] != 'instance':
            secret = read(backup['credentials_file'])
            if backup['type'] == 's3' and (set(secret) != {'access_key', 'secret_key'} or any(not isinstance(v, str) or not v or any(c in v for c in '\n\r\x00') for v in secret.values())): raise ValueError()
            if backup['type'] == 'gcs' and secret.get('type') != 'service_account': raise ValueError()
        if type(config.get('timeout_seconds', 1800)) is not int or not 60 <= config.get('timeout_seconds', 1800) <= 1800: raise ValueError()
        if config.get('data_device') and not re.fullmatch(r'/dev/[a-zA-Z0-9/_-]+', config['data_device']): raise ValueError()
    except (OSError, ValueError, TypeError, KeyError):
        return {'valid': False, 'missing': [], 'errors': ['DATABASE_CONFIG_INVALID']}
    return {'valid': True, 'missing': [], 'errors': [], 'live_verified': False}


def normalize(descriptor, config, context):
    if descriptor.get('schema_version') == 'v1':
        provider = descriptor.get('provider_kind')
        if provider not in ('aws', 'gcp') or descriptor.get('purpose') != 'database': raise StandaloneError('DATABASE_DESCRIPTOR_INVALID')
        ssh = config.get('ssh', descriptor.get('ssh'))
        request = ansible.from_descriptor(descriptor, request_id='database-preflight', operation='guest.check', ssh=ssh)
        node = request['inventory']['control_plane'][0]
        return {'version': 1, 'provider': provider, 'environment_id': context['environment_id'], 'account_id': context['account_id'], 'region': request['target']['placement'], 'resource_id': node['resource_id'], 'private_ipv4': node['private_ipv4'], 'ssh': node['ssh'], 'data_disk': descriptor.get('data_disk', {})}
    if descriptor.get('version') != 1 or descriptor.get('provider') not in ('openstack', 'ncp'): raise StandaloneError('DATABASE_DESCRIPTOR_INVALID')
    if descriptor.get('environment_id') != context['environment_id'] or descriptor.get('account_id') != context['account_id']: raise StandaloneError('DATABASE_DESCRIPTOR_SCOPE_MISMATCH')
    node = dict(descriptor)
    address = ipaddress.ip_address(node['private_ipv4'])
    if not any(address in private for private in ansible.PRIVATE): raise StandaloneError('DATABASE_PRIVATE_ADDRESS_REQUIRED')
    ssh = node['ssh']
    if set(ssh) - {'user', 'identity_file', 'known_hosts_file', 'port', 'connect_host'} or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', ssh['user']) or ssh.get('port', 22) != 22: raise StandaloneError('DATABASE_SSH_INVALID')
    if ssh.get('connect_host'):
        address = ipaddress.ip_address(ssh['connect_host'])
        private_relay = any(address in private for private in ansible.PRIVATE)
        ncp_pinned_public = node['provider'] == 'ncp' and ssh['connect_host'] == node.get('public_ipv4') and address.is_global
        if not private_relay and not ncp_pinned_public: raise StandaloneError('DATABASE_SSH_INVALID')
    return node


def restore_gate(operation, config, context, backups):
    backup_id = operation['backup_id']
    if context.get('restore_authorized') is not True or type(context.get('active_applications')) is not int or context.get('active_applications') != 0:
        raise StandaloneError('DATABASE_RESTORE_APPLICATIONS_ACTIVE')
    approval = read(config['restore_approval_file'])
    if (set(approval) != {'environment_id', 'backup_id', 'expires_at', 'approval_id'} or approval['environment_id'] != context['environment_id'] or approval['backup_id'] != backup_id or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{2,127}', approval['approval_id']) or dt.datetime.fromisoformat(approval['expires_at'].replace('Z', '+00:00')) <= dt.datetime.now(dt.timezone.utc)):
        raise StandaloneError('DATABASE_RESTORE_APPROVAL_REQUIRED')
    if backup_id not in backups: raise StandaloneError('DATABASE_BACKUP_PROOF_UNKNOWN')
    return backups[backup_id]


def run(action, descriptor, config, context, *, runner=ansible.execute):
    started = False
    try:
        operation = action_spec(action)
        if not validate_config(config)['valid']: raise StandaloneError('DATABASE_CONFIG_INVALID')
        node = normalize(descriptor, config, context)
        home = private_dir(Path(context['state_dir']) / 'standalone-postgres')
        fd = os.open(home / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            binding = {k: context[k] for k in ('environment_id', 'owner_id', 'account_id', 'credential_ref', 'policy_revision')}
            binding.update(resource_id=node['resource_id'], provider=node['provider'], descriptor=node, config=config)
            identity = digest(binding)
            if (home / 'binding.json').exists() and read(home / 'binding.json')['identity'] != identity: raise StandaloneError('DATABASE_AUTHORITY_CHANGED')
            save(home / 'binding.json', {'identity': identity})
            op_id = context.get('operation_id') or secrets.token_hex(16)
            if not re.fullmatch(r'[a-zA-Z0-9._-]{1,96}', op_id): raise StandaloneError('DATABASE_OPERATION_ID_INVALID')
            mutation = operation['action'] not in ('status', 'verify')
            journal = home / ('operation-' + op_id + '.json')
            fingerprint = digest({'identity': identity, 'operation': operation})
            if mutation and journal.exists():
                old = read(journal)
                if old['fingerprint'] != fingerprint: raise StandaloneError('DATABASE_IDEMPOTENCY_CONFLICT')
                if old['status'] == 'succeeded': return old['result']
                raise StandaloneError('DATABASE_RECONCILIATION_REQUIRED')
            pending_path = home / 'pending.json'
            pending = read(pending_path) if pending_path.exists() else None
            if mutation and pending: raise StandaloneError('DATABASE_RECONCILIATION_REQUIRED')
            backups = read(home / 'backups.json') if (home / 'backups.json').exists() else {}
            restore = restore_gate(operation, config, context, backups) if operation['action'] == 'restore' else None
            with ExitStack() as stack:
                work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='railshot-standalone-')))
                ssh = dict(node['ssh'])
                snapshots = {}
                refs = [ssh['identity_file'], ssh['known_hosts_file'], config['probe_password_file'], *(config['tls'][k] for k in ('ca_file', 'certificate_file', 'key_file'))]
                if config['backup'].get('credentials_file'): refs.append(config['backup']['credentials_file'])
                if operation['action'] == 'bind': refs.append(operation['binding_file'])
                for ref in refs:
                    dst = work / digest(ref); dst.write_bytes(private_bytes(ref)); dst.chmod(0o600); snapshots[ref] = str(dst)
                ssh.update(identity_file=snapshots[ssh['identity_file']], known_hosts_file=snapshots[ssh['known_hosts_file']])
                deadline = time.monotonic() + config.get('timeout_seconds', 1800)
                forwarded = stack.enter_context(ansible.forwarded_port(ssh.get('transport_ref'), deadline))
                request = {'inventory': {'control_plane': [{'id': 'database', 'private_ipv4': node['private_ipv4'], 'ssh': {**ssh, 'port': 22}}]}}
                host = ansible.build_inventory(request, forwarded)['all']['children']['k3s_server']['hosts']['database']
                inventory = work / 'inventory.json'; save(inventory, {'all': {'children': {'standalone_database': {'hosts': {'database': host}}}}})
                nonce = secrets.token_hex(32)
                guest = {'version': 1, 'operation': operation, 'fingerprint': fingerprint, 'pending_operation_id': pending['operation_id'] if pending else None, 'nonce': nonce, 'operation_id': op_id, 'environment_id': context['environment_id'], 'resource_id': node['resource_id'], 'private_ipv4': node['private_ipv4'], 'provider': node['provider'], 'data_disk': node.get('data_disk', {}), 'data_device': config.get('data_device'), 'client_cidrs': config['client_cidrs'], 'tls': {'server_name': config['tls']['server_name']}, 'backup': dict(config['backup']), 'probe_password': Path(snapshots[config['probe_password_file']]).read_text().strip(), 'restore': restore}
                for key in ('ca_file', 'certificate_file', 'key_file'): guest['tls'][key] = Path(snapshots[config['tls'][key]]).read_text()
                if config['backup'].get('credentials_file'): guest['backup']['credentials'] = read(snapshots[config['backup']['credentials_file']]); del guest['backup']['credentials_file']
                if operation['action'] == 'bind': guest['application_binding'] = read(snapshots[operation['binding_file']])
                guest_input = work / 'guest.json'; save(guest_input, guest)
                receipt = work / 'receipt.json'; variables = work / 'vars.json'
                save(variables, {'railshot_standalone_input': str(guest_input), 'railshot_standalone_receipt': str(receipt)})
                executable = shutil.which('ansible-playbook')
                if not executable: raise StandaloneError('ANSIBLE_UNAVAILABLE')
                if mutation:
                    save(pending_path, {'operation_id': op_id, 'fingerprint': fingerprint, 'action': operation['action']})
                    save(journal, {'status': 'unknown', 'fingerprint': fingerprint})
                started = True
                rc = runner([executable, '-i', str(inventory), str(HERE / 'standalone.yml'), '--extra-vars', '@' + str(variables)], max(1, deadline - time.monotonic()), ansible.child_env())
                if rc: raise StandaloneError('DATABASE_NATIVE_EXECUTION_FAILED')
                evidence = read(receipt)
                if (evidence.get('nonce') != nonce or evidence.get('environment_id') != context['environment_id'] or evidence.get('resource_id') != node['resource_id'] or evidence.get('ready') is not True or evidence.get('action') != operation['action'] or not re.fullmatch(r'[0-9]{10,30}', str(evidence.get('system_identifier', '')))):
                    raise StandaloneError('DATABASE_READINESS_UNPROVEN')
                if operation['action'] in ('install', 'backup') and not evidence.get('backup'):
                    raise StandaloneError('DATABASE_BACKUP_UNPROVEN')
                recovered = evidence.get('reconciled', {})
                if recovered and (not pending or recovered.get('fingerprint') != pending['fingerprint'] or recovered.get('status') != 'succeeded' or recovered.get('result', {}).get('environment_id') != context['environment_id'] or recovered.get('result', {}).get('resource_id') != node['resource_id'] or recovered.get('result', {}).get('ready') is not True):
                    raise StandaloneError('DATABASE_RECONCILIATION_UNPROVEN')
                backup_evidence = evidence.get('backup') or recovered.get('result', {}).get('backup')
                if backup_evidence:
                    proof = backup_evidence; backup_id = proof.get('backup_id', '')
                    if not re.fullmatch(BACKUP_ID, backup_id) or proof.get('system_identifier') != evidence['system_identifier'] or not re.fullmatch(r'[a-f0-9]{64}', proof.get('proof', '')): raise StandaloneError('DATABASE_BACKUP_UNPROVEN')
                    backups[backup_id] = proof; save(home / 'backups.json', backups)
                result = {'status': 'succeeded', 'ready': True, 'database_ready': True, 'system_identifier': evidence['system_identifier'], 'backups': sorted(backups), 'receipt_path': str(home / 'last-receipt.json')}
                if operation['action'] == 'bind': result['binding_file'] = operation['binding_file']
                if evidence.get('retained_data_path'): result['retained_data_path'] = evidence['retained_data_path']
                save(home / 'last-receipt.json', evidence)
                if mutation:
                    save(journal, {'status': 'succeeded', 'fingerprint': fingerprint, 'result': result})
                    pending_path.unlink()
                elif recovered:
                    save(home / ('operation-' + pending['operation_id'] + '.json'), {'status': 'succeeded', 'fingerprint': pending['fingerprint'], 'result': result})
                    pending_path.unlink()
                elif pending:
                    result.update(status='unknown', ready=False, database_ready=False, error={'code': 'DATABASE_RECONCILIATION_REQUIRED', 'outcome_unknown': True})
                return result
        finally: os.close(fd)
    except Exception as error:
        return {'status': 'unknown' if started else 'blocked', 'ready': False, 'database_ready': False, 'error': {'code': getattr(error, 'code', 'DATABASE_NATIVE_OUTCOME_UNKNOWN' if started else 'DATABASE_PREFLIGHT_FAILED'), 'outcome_unknown': started}}


def config_template(directory):
    root = Path(directory)
    config = {'version': 1, 'client_cidrs': [], 'data_device': '',
        'ssh': {'user': 'ubuntu', 'identity_file': '', 'known_hosts_file': '', 'port': 22},
        'tls': {'server_name': '', 'ca_file': '', 'certificate_file': '', 'key_file': ''},
        'probe_password_file': str(root / 'postgres-probe-password.txt'),
        'backup': {'type': 's3', 'bucket': '', 'prefix': '/railshot/postgres', 'region': '', 'endpoint': '', 'identity': 'instance', 'retention_full': 7},
        'restore_approval_file': str(root / 'postgres-restore-approval.json'), 'timeout_seconds': 1800}
    return {'config': config, 'files': {'postgres-config.json': config,
        'postgres-restore-approval.json': {'approval_id': '', 'environment_id': '', 'backup_id': '', 'expires_at': ''}},
        'notes': ['Supply private TLS files and a random probe password of at least 24 characters.',
            'Instance identity requires bucket-scoped write/read permissions and cloud OAuth scope for GCS.',
            'Secret identity supports S3-compatible NCP/onprem; credentials_file contains access_key/secret_key.',
            'Restore approval is blank intentionally and is not authorization. Applications must be stopped by the common control plane.']}


def application_bundle(descriptor, config_file, context):
    """Persist references for the existing app registration path, never credentials."""
    home = private_dir(Path(context['state_dir']) / 'standalone-postgres')
    config = read(config_file)
    node = normalize(descriptor, config, context)
    # Keep the original descriptor for native AWS/GCP transport normalization.
    save(home / 'node.json', descriptor)
    safe_context = {key: context[key] for key in ('environment_id', 'owner_id', 'account_id', 'credential_ref', 'policy_revision', 'state_dir')}
    save(home / 'context.json', safe_context)
    bundle = home / 'application-bundle.json'
    save(bundle, {'version': 1, 'descriptor_file': str(home / 'node.json'), 'config_file': str(config_file), 'context_file': str(home / 'context.json')})
    return str(bundle)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Offline standalone PostgreSQL configuration validation')
    parser.add_argument('--config', required=True)
    parser.add_argument('--validate-only', action='store_true', required=True)
    args = parser.parse_args()
    try: result = validate_config(read(args.config))
    except Exception: result = {'valid': False, 'missing': [], 'errors': ['DATABASE_CONFIG_FILE_INVALID']}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['valid'] else 1


if __name__ == '__main__': raise SystemExit(main())
