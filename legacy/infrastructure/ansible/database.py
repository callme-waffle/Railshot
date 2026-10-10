"""Approved HA profiles -> existing team playbook; no caller-supplied paths or vars."""
from contextlib import ExitStack
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import time

import yaml
import inputs
import run as ansible

GROUPS = {'database': 'db_nodes', 'dcs': 'etcd_nodes', 'proxy': 'proxy_nodes'}
TLS = {'dcs': {'etcd_ca_src', 'etcd_cert_src', 'etcd_key_src'},
       'database': {'etcd_ca_src', 'patroni_etcd_cert_src', 'patroni_etcd_key_src',
                    'patroni_api_ca_src', 'patroni_api_cert_src', 'patroni_api_key_src',
                    'postgres_tls_ca_src', 'postgres_tls_cert_src', 'postgres_tls_key_src'},
       'proxy': {'patroni_api_ca_src'}}
SECRETS = {'vault_postgres_password', 'vault_replication_password', 'vault_patroni_api_password'}


class ProfileError(ValueError):
    def __init__(self, code, status=503):
        self.code, self.status = code, status
        super().__init__(code)


def private_bytes(path):
    if not isinstance(path, str) or not Path(path).is_absolute() or any(x in path for x in ('{{', '{%', '\x00')):
        raise ValueError('private absolute file reference required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private executor-owned regular file required')
        data = stream.read(ansible.MAX_BYTES + 1)
    if not 0 < len(data) <= ansible.MAX_BYTES:
        raise ValueError('private file size invalid')
    return data


def topology(parameters):
    return {'mode': parameters['mode'],
            'nodes': sorted(({'target_id': n['target_id'], 'roles': sorted(n['roles'])}
                             for n in parameters['nodes']), key=lambda n: n['target_id']),
            'placements': sorted(({**p, 'proxy_nodes': p.get('proxy_nodes', 0)}
                                  for p in parameters['placements']), key=lambda p: (p['provider'], p['site']))}


def prepare(body, profiles, resolve):
    parameters = inputs.validate_job(body)
    nodes = {}
    def registered(target_id):
        nodes[target_id] = resolve(target_id)
        return nodes[target_id]
    plan = inputs.database_plan(body, parameters, registered)
    if plan['blockers'][0]['code'] != 'DATABASE_PROFILE_REQUIRED' or not parameters.get('profile_id'):
        return plan, None
    path = profiles.get(parameters['profile_id'])
    if path is None:
        raise ProfileError('DATABASE_PROFILE_NOT_REGISTERED', 400)
    try:
        raw = private_bytes(path)
        profile = json.loads(raw, object_pairs_hook=ansible.unique_pairs)
        required = {'parameters', 'cluster_name', 'client_cidrs', 'certificates',
                    'vault_file', 'vault_password_file', 'timeout_seconds'}
        backup_fields = {'backup_enabled', 'pgbackrest_repo_path', 'retention_full'}
        optional = backup_fields | {'require_postgres_mount'}
        if not isinstance(profile, dict) or not required <= set(profile) or set(profile) - required - optional:
            raise ValueError('invalid profile fields')
        if type(profile.get('backup_enabled', False)) is not bool:
            raise ValueError('invalid backup policy')
        if type(profile.get('require_postgres_mount', False)) is not bool:
            raise ValueError('invalid mount policy')
        if profile.get('backup_enabled'):
            if not backup_fields <= set(profile) or not re.fullmatch(r'/mnt/[A-Za-z0-9_/-]+', profile['pgbackrest_repo_path']):
                raise ValueError('approved mounted backup path required')
            if type(profile['retention_full']) is not int or not 1 <= profile['retention_full'] <= 30:
                raise ValueError('invalid backup retention')
        inputs.validate_job({**body, 'parameters': profile['parameters']})
        if topology(profile['parameters']) != topology(parameters):
            raise ProfileError('DATABASE_PROFILE_MISMATCH', 400)
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', profile['cluster_name']):
            raise ValueError('invalid cluster name')
        if type(profile['timeout_seconds']) is not int or not 30 <= profile['timeout_seconds'] <= 1800:
            raise ValueError('invalid deadline')
        if not isinstance(profile['client_cidrs'], list) or not 1 <= len(profile['client_cidrs']) <= 64:
            raise ValueError('client networks required')
        for cidr in profile['client_cidrs']:
            network = ipaddress.IPv4Network(cidr, strict=True)
            if not any(network.subnet_of(private) for private in ansible.PRIVATE):
                raise ValueError('approved private client networks required')
        certificates = profile['certificates']
        if not isinstance(certificates, dict) or set(certificates) != set(nodes):
            raise ValueError('certificate node binding mismatch')
        files = {path: hashlib.sha256(raw).hexdigest()}
        for selection in parameters['nodes']:
            target_id = selection['target_id']
            required_tls = set().union(*(TLS[role] for role in selection['roles']))
            if not isinstance(certificates[target_id], dict) or set(certificates[target_id]) != required_tls:
                raise ValueError('certificate role binding mismatch')
            ssh = nodes[target_id]['inventory']['control_plane'][0]['ssh']
            for reference in [*certificates[target_id].values(), ssh['identity_file'], ssh['known_hosts_file']]:
                files[reference] = hashlib.sha256(private_bytes(reference)).hexdigest()
        for key in ('vault_file', 'vault_password_file'):
            value = private_bytes(profile[key])
            if key == 'vault_file' and not value.startswith(b'$ANSIBLE_VAULT;'):
                raise ValueError('encrypted Ansible Vault required')
            files[profile[key]] = hashlib.sha256(value).hexdigest()
    except ProfileError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise ProfileError('DATABASE_PROFILE_INVALID') from None
    request = {'schema_version': '1.0', 'request_id': body['request_id'], 'operation': 'database.configure',
               'target': nodes[body['target_id']]['target'], 'timeout_seconds': profile['timeout_seconds'],
               'database_nodes': [nodes[key] for key in sorted(nodes)], 'profile': profile,
               'profile_id': parameters['profile_id'], 'file_sha256': files}
    plan.update(execution_supported=True, blockers=[])
    return plan, request


def result_for(request):
    return {**ansible.base_result(request), 'database_ready': False}


def decrypt_vault(vault, password, timeout):
    executable = shutil.which('ansible-vault')
    if not executable:
        raise ValueError('vault executable unavailable')
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen([executable, 'view', '--vault-password-file', str(password), str(vault)],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
            env=ansible.child_env(), start_new_session=True)
        try:
            if process.wait(timeout=timeout):
                raise ValueError('vault decryption failed')
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        output.seek(0)
        raw = output.read(ansible.MAX_BYTES + 1)
    if len(raw) > ansible.MAX_BYTES:
        raise ValueError('vault payload too large')
    values = yaml.safe_load(raw)
    if not isinstance(values, dict) or set(values) != SECRETS or any(
            not isinstance(value, str) or len(value) < 16 or any(x in value for x in ('{{', '{%', '\x00'))
            for value in values.values()):
        raise ValueError('vault must contain only the three credential strings')
    return values


def _run(request, runner):
    result = result_for(request)
    result['stage'] = 'executor_preflight'
    deadline = time.monotonic() + request['timeout_seconds']
    started = False
    try:
        with ExitStack() as stack:
            work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='railshot-db-')))
            snapshots = {}
            for reference, expected in request['file_sha256'].items():
                data = private_bytes(reference)
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ProfileError('DATABASE_PROFILE_CHANGED')
                snapshot = work / ('ref-' + hashlib.sha256(reference.encode()).hexdigest())
                snapshot.write_bytes(data); snapshot.chmod(0o600)
                snapshots[reference] = str(snapshot)
            profile = request['profile']
            credentials = decrypt_vault(snapshots[profile['vault_file']], snapshots[profile['vault_password_file']],
                                        max(0.001, deadline - time.monotonic()))
            groups = {name: {'hosts': {}} for name in GROUPS.values()}
            selections = {item['target_id']: item['roles'] for item in profile['parameters']['nodes']}
            for original in request['database_nodes']:
                node_request = copy.deepcopy(original)
                node = node_request['inventory']['control_plane'][0]
                ssh = node['ssh']
                ssh['identity_file'] = snapshots[ssh['identity_file']]
                ssh['known_hosts_file'] = snapshots[ssh['known_hosts_file']]
                forwarded = stack.enter_context(ansible.forwarded_port(ssh.get('transport_ref'), deadline))
                host = ansible.build_inventory(node_request, forwarded)['all']['children']['k3s_server']['hosts'][node['id']]
                host.update(private_ip=node['private_ipv4'], site=node_request['target']['placement'])
                host.update({key: snapshots[value] for key, value in profile['certificates'][node['id']].items()})
                for role in selections[node['id']]:
                    groups[GROUPS[role]]['hosts'][node['id']] = host
            inventory = work / 'inventory.json'
            inventory.write_text(json.dumps({'all': {'children': groups}})); inventory.chmod(0o600)
            nonce = secrets.token_hex(16)
            receipt = work / 'database-receipt.json'
            expected = {'request_id': request['request_id'], 'target_id': request['target']['id'],
                        'nonce': nonce, 'stage': 'database', 'database_ready': True,
                        'target_ids': sorted(selections)}
            variables = work / 'vars.json'
            variables.write_text(json.dumps({**credentials, 'cluster_name': profile['cluster_name'],
                'client_cidrs': profile['client_cidrs'], 'backup_enabled': profile.get('backup_enabled', False),
                'railshot_require_postgres_mount': profile.get('require_postgres_mount', False),
                **{key: profile[key] for key in ('pgbackrest_repo_path', 'retention_full') if key in profile},
                'railshot_database_receipt': expected, 'railshot_database_receipt_path': str(receipt)}))
            variables.chmod(0o600)
            executable = shutil.which('ansible-playbook')
            if not executable:
                raise ProfileError('ANSIBLE_UNAVAILABLE')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired('database', request['timeout_seconds'])
            result['stage'] = 'database'
            started = True
            rc = runner([executable, '-i', str(inventory), str(ansible.HERE / 'database.yml'),
                         '--extra-vars', '@' + str(variables)], remaining, ansible.child_env())
            result['steps'].append({'stage': 'database', 'exit_code': rc})
            if rc:
                return ansible.fail(result, 'failed', 'DATABASE_CONFIGURE_FAILED',
                                    'HA playbook failed; reconcile the selected targets', unknown=True)
            if json.loads(receipt.read_text()) != expected:
                raise ValueError('readiness receipt mismatch')
            result.update(status='succeeded', guest_ready=True, database_ready=True)
            result['steps'][-1]['receipt'] = expected
            return result
    except subprocess.TimeoutExpired:
        return ansible.fail(result, 'failed' if started else 'blocked', 'EXECUTION_TIMEOUT',
                            'Database execution deadline exceeded', unknown=started)
    except ProfileError as exc:
        return ansible.fail(result, 'blocked', exc.code, 'Approved database configuration is unavailable')
    except (OSError, ValueError, TypeError, KeyError):
        return ansible.fail(result, 'failed' if started else 'blocked',
                            'READINESS_UNPROVEN' if started else 'DATABASE_PREFLIGHT_FAILED',
                            'Database configuration or native completion is unavailable', unknown=started)


def run(request, *, state_dir=None, runner=ansible.execute):
    return ansible.run_locked(request, result_for(request), ansible.lock_keys(request),
                              lambda: _run(request, runner), state_dir=state_dir)
