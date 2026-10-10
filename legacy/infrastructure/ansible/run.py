#!/usr/bin/env python3
"""Trusted local adapter. JSON only; no arbitrary playbooks, variables or shell."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
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
import sys
import tempfile
import time
from transport import forwarded_port, transport_parts

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'ci/scripts'))
from storage import durable_write
SCHEMA = ROOT / 'contracts/ansible-request.schema.json'
PLAYBOOKS = {'guest': HERE / 'guest.yml', 'runtime': HERE / 'runtime.yml'}
PRIVATE = tuple(ipaddress.ip_network(x) for x in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
MAX_BYTES = 1024 * 1024
STATE_DIR = Path.home() / '.local/state/railshot/ansible'


class ContractError(ValueError):
    pass


def check_schema(value, rule, path='$'):
    """Validate the deliberately small, checked-in JSON Schema subset."""
    kind = rule.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int}
    if kind and type(value) is not types[kind]:
        raise ContractError(f'{path}: invalid type')
    if 'const' in rule and value != rule['const']:
        raise ContractError(f'{path}: invalid constant')
    if 'enum' in rule and value not in rule['enum']:
        raise ContractError(f'{path}: unsupported value')
    if kind == 'object':
        props = rule.get('properties', {})
        if not set(rule.get('required', [])).issubset(value):
            raise ContractError(f'{path}: missing required field')
        if rule.get('additionalProperties') is False and set(value) - set(props):
            raise ContractError(f'{path}: unknown field')
        for key, child in value.items():
            if key in props:
                check_schema(child, props[key], f'{path}.{key}')
    if kind == 'array':
        if not rule.get('minItems', 0) <= len(value) <= rule.get('maxItems', MAX_BYTES):
            raise ContractError(f'{path}: invalid item count')
        for child in value:
            check_schema(child, rule['items'], f'{path}[]')
    if kind == 'string':
        if not rule.get('minLength', 0) <= len(value) <= rule.get('maxLength', MAX_BYTES):
            raise ContractError(f'{path}: invalid length')
        if 'pattern' in rule and re.fullmatch(rule['pattern'], value) is None:
            raise ContractError(f'{path}: invalid format')
    if kind == 'integer' and not rule.get('minimum', -MAX_BYTES) <= value <= rule.get('maximum', MAX_BYTES):
        raise ContractError(f'{path}: outside allowed range')
    # Operation-specific inputs use the checked-in if/then/else subset.
    for conditional in rule.get('allOf', []):
        expected = conditional['if']['properties']['operation']['const']
        branch = conditional['then'] if value.get('operation') == expected else conditional['else']
        if not set(branch.get('required', [])).issubset(value):
            raise ContractError(f'{path}: missing operation input')
        if 'not' in branch and set(branch['not']['required']).issubset(value):
            raise ContractError(f'{path}: unexpected operation input')
        for key, child in branch.get('properties', {}).items():
            if key in value:
                check_schema(value[key], child, f'{path}.{key}')


def validate(request):
    check_schema(request, json.loads(SCHEMA.read_text()))
    inventory = request.get('inventory', {})
    nodes = inventory.get('control_plane', []) + inventory.get('workers', [])
    ids, addresses, resources = set(), set(), set()
    for node in nodes:
        try:
            addr = ipaddress.IPv4Address(node['private_ipv4'])
        except ipaddress.AddressValueError as exc:
            raise ContractError('inventory: invalid IPv4 address') from exc
        if not any(addr in network for network in PRIVATE):
            raise ContractError('inventory: RFC1918 private IPv4 required')
        if node['id'] in ('all', 'ungrouped') or node['id'] in ids or str(addr) in addresses or node['resource_id'] in resources:
            raise ContractError('inventory: duplicate node identity, resource or address')
        ids.add(node['id']); addresses.add(str(addr)); resources.add(node['resource_id'])
        for field in ('identity_file', 'known_hosts_file'):
            p = Path(node['ssh'][field])
            if '..' in p.parts:
                raise ContractError('ssh: parent traversal is not allowed')
        reference = node['ssh'].get('transport_ref')
        if 'connect_host' in node['ssh']:
            try:
                connect = ipaddress.IPv4Address(node['ssh']['connect_host'])
            except ipaddress.AddressValueError as exc:
                raise ContractError('ssh: invalid relay IPv4 address') from exc
            if (request['target']['provider'] != 'openstack' or reference is not None
                    or not any(connect in network for network in PRIVATE)):
                raise ContractError('ssh: relay requires an OpenStack RFC1918 address without transport_ref')
        if reference is not None:
            kind, *parts = transport_parts(reference)
            if node['ssh']['port'] != 22:
                raise ContractError('cloud tunnel: only guest SSH port 22 is supported')
            if kind == 'ssm':
                region, instance = parts
                expected = f'arn:aws:ec2:{region}:'
                if request['target']['provider'] != 'aws' or request['target']['placement'] != region or not (
                        node['resource_id'] == instance or (node['resource_id'].startswith(expected)
                        and node['resource_id'].endswith('/' + instance))):
                    raise ContractError('SSM transport does not match the target resource')
            else:
                project, zone, instance = parts
                if request['target']['provider'] != 'gcp' or request['target']['placement'] != zone or (
                        node['resource_id'] != f'projects/{project}/zones/{zone}/instances/{instance}'):
                    raise ContractError('IAP transport does not match the target resource')
    placements = request.get('patroni', {}).get('placements', [])
    seen = set()
    for placement in placements:
        key = (placement['provider'], placement['site'])
        if key in seen or placement['database_nodes'] + placement['dcs_voters'] == 0:
            raise ContractError('patroni: duplicate or empty placement')
        seen.add(key)
    if placements and not sum(p['database_nodes'] for p in placements):
        raise ContractError('patroni: at least one database node must be requested')
    return request


def base_result(request=None):
    request = request or {}
    return {'schema_version': '1.0', 'request_id': request.get('request_id'),
            'target_id': request.get('target', {}).get('id'), 'operation': request.get('operation'),
            'status': 'failed', 'stage': 'validation', 'guest_ready': False,
            'runtime_ready': False, 'application_ready': False, 'public_http_verified': False,
            'steps': [], 'replayed': False, 'error': None}


def fail(result, status, code, message, *, unknown=False):
    result.update(status=status, error={'code': code, 'message': message,
                                       'retryable': False, 'outcome_unknown': unknown})
    return result


def support_error(request):
    if request['operation'] == 'patroni.install':
        return ('PATRONI_PLAYBOOK_UNAVAILABLE', 'Placement is recorded; the team Patroni playbook is not connected to this executor.')
    inv = request['inventory']
    if len(inv['control_plane']) != 1 or inv['workers']:
        return ('SINGLE_NODE_ONLY', 'Current runtime supports one control-plane node and no workers.')
    if request['operation'] == 'runtime.install' and request['target']['architecture'] != 'amd64':
        return ('RUNTIME_ARCHITECTURE_UNVERIFIED', 'The first integrated runtime profile is amd64; ARM64 remains unverified.')
    return None


def private_file(path, *, identity=False):
    p = Path(path)
    try:
        info = p.lstat()
    except OSError as exc:
        raise ContractError('SSH identity/known-hosts reference is unavailable') from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size == 0:
        raise ContractError('SSH references must be nonempty regular files owned by the executor')
    if info.st_mode & (0o077 if identity else 0o022):
        raise ContractError('SSH reference file permissions are too broad')


def build_inventory(request, forwarded=None):
    node = request['inventory']['control_plane'][0]
    ssh = node['ssh']
    # A controller-owned localhost tunnel does not permit ProxyCommand/config inheritance.
    common = ('-F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none '
              '-o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null '
              '-o ProxyCommand=none -o ProxyJump=none -o ForwardAgent=no '
              '-o ClearAllForwardings=yes -o PasswordAuthentication=no '
              '-o KbdInteractiveAuthentication=no -o GSSAPIAuthentication=no '
              f'-o UserKnownHostsFile={ssh["known_hosts_file"]}')
    if forwarded is not None or 'connect_host' in ssh:
        common += f' -o HostKeyAlias={node["private_ipv4"]}'
    host = {'ansible_host': '127.0.0.1' if forwarded is not None else ssh.get('connect_host', node['private_ipv4']), 'ansible_user': ssh['user'],
            'ansible_port': forwarded or ssh['port'], 'ansible_connection': 'ssh',
            'ansible_ssh_private_key_file': ssh['identity_file'],
            'ansible_ssh_common_args': common,
            'ansible_become_method': 'sudo',
            'ansible_become_flags': '-H -S -n'}
    return {'all': {'children': {'k3s_server': {'hosts': {node['id']: host}},
                                 'k3s_workers': {'hosts': {}}}}}


def playbook_variables(request):
    """Map the public contract to the variables the existing team playbooks consume."""
    node = request['inventory']['control_plane'][0]
    return {'railshot_request_id': request['request_id'], 'railshot_target_id': request['target']['id'],
            'railshot_resource_id': node['resource_id'],
            'railshot_expected_arch': request['target']['architecture'],
            'railshot_initialization': request['target']['initialization'],
            'railshot_node_ip': node['private_ipv4'], 'k3s_api_host': node['private_ipv4'],
            'k3s_node_name': node['id'], 'k3s_version': 'v1.34.11+k3s1',
            'railshot_wait_timeout_seconds': request.get('runtime', {}).get('wait_timeout_seconds', 300)}


def child_env():
    # Do not inherit arbitrary Ansible plugins, extra config, SSH agent or cloud credentials.
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}
    env.update(LANG='en_US.UTF-8' if sys.platform == 'darwin' else 'C.UTF-8',
               LC_ALL='en_US.UTF-8' if sys.platform == 'darwin' else 'C.UTF-8',
               ANSIBLE_CONFIG=str(HERE / 'ansible.cfg'), ANSIBLE_HOST_KEY_CHECKING='True',
               ANSIBLE_RETRY_FILES_ENABLED='False', ANSIBLE_NOCOLOR='True',
               ANSIBLE_SSH_ARGS='-o ControlMaster=no', ANSIBLE_BECOME_ASK_PASS='False')
    return env


def execute(argv, timeout, env):
    """Bound the complete local process group; never echo potentially sensitive output."""
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(argv, cwd=HERE, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)
        try:
            return proc.wait(timeout=timeout)
        finally:
            # Also reap children after interruption or a parent that exits early.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def read_receipt(path, request, stage, nonce):
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ContractError('Playbook did not produce a valid readiness receipt') from exc
    expected = {'request_id': request['request_id'], 'target_id': request['target']['id'],
                'node_id': request['inventory']['control_plane'][0]['id'],
                'stage': stage, 'nonce': nonce}
    if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()) or receipt.get(stage + '_ready') is not True:
        raise ContractError('Readiness receipt does not match this request and stage')
    return {**expected, stage + '_ready': True}


def _run(request, *, validate_only=False, runner=execute):
    try:
        validate(request)
    except (ContractError, OSError, ValueError) as exc:
        return fail(base_result(), 'invalid', 'INVALID_REQUEST', str(exc))
    result = base_result(request)
    blocked = support_error(request)
    if blocked:
        return fail(result, 'blocked', *blocked)
    if validate_only:
        result['status'] = 'validated'
        return result
    result['stage'] = 'executor_preflight'
    node = request['inventory']['control_plane'][0]
    try:
        private_file(node['ssh']['identity_file'], identity=True)
        private_file(node['ssh']['known_hosts_file'])
    except ContractError as exc:
        return fail(result, 'blocked', 'SSH_REFERENCE_UNAVAILABLE', str(exc))
    executable = shutil.which('ansible-playbook')
    if not executable:
        return fail(result, 'blocked', 'ANSIBLE_UNAVAILABLE', 'ansible-playbook is not installed on the executor')
    deadline = time.monotonic() + request['timeout_seconds']
    with ExitStack() as stack:
        try:
            forwarded = stack.enter_context(forwarded_port(node['ssh'].get('transport_ref'), deadline))
        except (OSError, ValueError) as exc:
            return fail(result, 'blocked', 'TRANSPORT_UNAVAILABLE', str(exc))
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix='railshot-ansible-'))
        work = Path(directory)
        inventory = work / 'inventory.json'
        inventory.write_text(json.dumps(build_inventory(request, forwarded)))
        inventory.chmod(0o600)
        for stage in ('guest', 'runtime') if request['operation'] == 'runtime.install' else ('guest',):
            result['stage'] = stage
            nonce = secrets.token_hex(16)
            receipt_path = work / f'{stage}-receipt.json'
            extra = {**playbook_variables(request), 'railshot_nonce': nonce,
                     'railshot_receipt_path': str(receipt_path)}
            extra_path = work / f'{stage}-vars.json'
            extra_path.write_text(json.dumps(extra)); extra_path.chmod(0o600)
            argv = [executable, '-i', str(inventory), str(PLAYBOOKS[stage]), '--extra-vars', '@' + str(extra_path)]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return fail(result, 'failed', 'EXECUTION_TIMEOUT', 'Request deadline exceeded', unknown=stage == 'runtime')
            try:
                rc = runner(argv, remaining, child_env())
            except subprocess.TimeoutExpired:
                return fail(result, 'failed', 'EXECUTION_TIMEOUT', 'Playbook deadline exceeded; inspect target state', unknown=stage == 'runtime')
            except OSError:
                return fail(result, 'failed', 'EXECUTOR_FAILURE', 'Unable to start the trusted Ansible executable')
            result['steps'].append({'stage': stage, 'exit_code': rc})
            if rc:
                code = 'GUEST_CHECK_FAILED' if stage == 'guest' else 'RUNTIME_INSTALL_FAILED'
                return fail(result, 'failed', code, 'Playbook failed; readiness was not established', unknown=stage == 'runtime')
            try:
                result['steps'][-1]['receipt'] = read_receipt(receipt_path, request, stage, nonce)
            except ContractError as exc:
                return fail(result, 'failed', 'READINESS_UNPROVEN', str(exc), unknown=stage == 'runtime')
            result[stage + '_ready'] = True
    result['status'] = 'succeeded'
    return result


def run(request, *, validate_only=False, runner=execute, state_dir=None):
    """Persist once-only request admission and lock both the logical and physical target."""
    preview = _run(request, validate_only=True)
    if validate_only or preview['status'] != 'validated':
        return preview
    return run_locked(request, base_result(request), lock_keys(request),
                      lambda: _run(request, runner=runner), state_dir=state_dir)


def normalize_lock_keys(keys):
    """Keep legacy locks and join AWS ARN/raw-ID aliases, including saved unknown jobs."""
    if not isinstance(keys, (list, tuple, set)) or any(not isinstance(key, str) for key in keys):
        raise ContractError('Invalid saved resource locks')
    result = set(keys)
    for key in keys:
        arn = re.fullmatch(r'resource:arn:aws:ec2:([a-z]{2}(?:-[a-z]+)+-\d):[0-9]{12}:instance/(i-[a-f0-9]{8,17})', key)
        if arn:
            region, instance = arn.groups()
            result.update(('resource:' + instance, f'transport:ssm:{region}:{instance}'))
    return sorted(result)


def lock_keys(request):
    keys = []
    for node in request.get('database_nodes', [request]):
        physical = node['inventory']['control_plane'][0]
        keys.extend(('target:' + node['target']['id'], 'resource:' + physical['resource_id']))
        reference = physical['ssh'].get('transport_ref')
        if reference:
            kind, *parts = transport_parts(reference)
            keys.append('transport:' + reference)
            if kind == 'ssm':
                # Preserve raw-ID locks used before canonical transport identities existed.
                keys.append('resource:' + parts[1])
    return normalize_lock_keys(keys)


def run_locked(request, result, identities, operation, *, state_dir=None):
    """Shared admission for one runtime or every physical member of a DB cluster."""
    directory = Path(state_dir) if state_dir is not None else STATE_DIR
    execution_started = False
    try:
        identities = normalize_lock_keys(identities)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ContractError('State directory must be private and owned by the executor')
        with ExitStack() as stack:
            for identity in sorted(set(identities) | {'request:' + request['request_id']}):
                path = directory / (hashlib.sha256(identity.encode()).hexdigest() + '.lock')
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                lock = stack.enter_context(os.fdopen(fd, 'r+'))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return fail(result, 'blocked', 'TARGET_BUSY', 'Another request owns this target')
            path = directory / (hashlib.sha256(request['request_id'].encode()).hexdigest() + '.json')
            digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if path.exists() or path.is_symlink():
                private_file(path, identity=True)
                prior = json.loads(path.read_text())
                if not isinstance(prior, dict) or set(prior) not in (
                        {'request_sha256', 'result'}, {'request_sha256', 'result', 'lock_keys'}):
                    raise ContractError('Invalid saved request record')
                if prior.get('request_sha256') != digest:
                    return fail(result, 'blocked', 'REQUEST_ID_CONFLICT', 'Request ID already names different inputs')
                if prior.get('result') is not None:
                    saved = prior['result']
                    if not isinstance(saved, dict) or set(saved) != set(result) or saved.get('status') not in (
                            'succeeded', 'invalid', 'blocked', 'failed') or saved.get('request_id') != request['request_id']:
                        raise ContractError('Invalid saved request result')
                    return {**prior['result'], 'replayed': True}
                return fail(result, 'blocked', 'PREVIOUS_OUTCOME_UNKNOWN', 'Previous executor did not record completion; inspect target before a new request', unknown=True)
            for previous in directory.glob('*.json'):
                private_file(previous, identity=True)
                saved = json.loads(previous.read_text())
                if not isinstance(saved, dict) or 'result' not in saved:
                    raise ContractError('Invalid saved request record')
                saved_result = saved['result']
                error = (saved_result or {}).get('error') or {}
                incomplete = saved_result is None or error.get('outcome_unknown') is True
                if incomplete and (not saved.get('lock_keys') or set(identities) & set(normalize_lock_keys(saved['lock_keys']))):
                    return fail(result, 'blocked', 'PREVIOUS_OUTCOME_UNKNOWN',
                                'A previous request has unresolved effects on a selected resource', unknown=True)
            durable_write(path, json.dumps({'request_sha256': digest, 'result': None,
                                           'lock_keys': identities}).encode())
            execution_started = True
            result = operation()
            durable_write(path, json.dumps({'request_sha256': digest, 'result': result,
                                           'lock_keys': identities}).encode())
            return result
    except (OSError, ValueError):
        return fail(result, 'failed' if execution_started else 'blocked', 'JOB_RECORD_UNAVAILABLE',
                    'Unable to read or persist the private job record',
                    unknown=execution_started and request['operation'] != 'guest.check')


def from_descriptor(descriptor, *, request_id, operation, ssh, timeout_seconds=1200):
    """Bind registered Terraform/AWS CLI references; not a live readiness observation."""
    try:
        if descriptor['schema_version'] != 'v1' or not (
                descriptor['execution_driver'] == 'terraform' or
                (descriptor['execution_driver'] == 'aws-cli' and descriptor.get('provider_kind') == 'aws')):
            raise ContractError('Unsupported node descriptor version or execution driver')
        provider = descriptor['provider_kind']
        if provider not in ('aws', 'gcp') or descriptor['architecture'] != 'x86_64':
            raise ContractError('Descriptor adapter supports AWS/GCP amd64 only')
        reference = descriptor['transport_ref']
        transport_parts(reference)
        target = descriptor['target_id']
        request = {'schema_version': '1.0', 'request_id': request_id, 'operation': operation,
                   'target': {'id': target, 'provider': provider, 'os': 'linux', 'architecture': 'amd64',
                              'placement': descriptor['location']['region' if provider == 'aws' else 'zone'],
                              'initialization': 'cloud-init' if provider == 'aws' else 'preconfigured'},
                   'inventory': {'control_plane': [{'id': target, 'resource_id': descriptor['resource_id'],
                                  'private_ipv4': descriptor['addresses']['private'],
                                  'ssh': {**ssh, 'port': 22, 'transport_ref': reference}}], 'workers': []},
                   'timeout_seconds': timeout_seconds}
        return validate(request)
    except (KeyError, TypeError) as exc:
        raise ContractError('Incomplete node descriptor or SSH reference') from exc


def from_openstack(server, *, request_id, operation, target_id, resource_id, project_id,
                   management_network, placement, architecture, initialization, ssh, timeout_seconds=1200):
    """Consume the Controller's GET ServerResponse, never its create/202 receipt.

    The trusted caller obtains this response with the intended project credential.
    Matching fields is not an independent ownership lookup or an SSH readiness check.
    """
    if (not all(isinstance(v, str) and v.strip() for v in (resource_id, project_id, management_network))
            or not isinstance(server, dict) or server.get('id') != resource_id
            or server.get('project_id') != project_id or server.get('status') != 'ACTIVE'):
        raise ContractError('OpenStack server ID, project or ACTIVE state does not match')
    addresses = server.get('addresses')
    if not isinstance(addresses, list):
        raise ContractError('OpenStack ServerResponse addresses required')
    matches = [row.get('address') for row in addresses if isinstance(row, dict)
               and row.get('network') == management_network and type(row.get('version')) is int
               and row['version'] == 4]
    if len(matches) != 1:
        raise ContractError('Exactly one IPv4 address on the approved management network is required')
    return validate({'schema_version': '1.0', 'request_id': request_id, 'operation': operation,
                     'target': {'id': target_id, 'provider': 'openstack', 'placement': placement,
                                'os': 'linux', 'architecture': architecture, 'initialization': initialization},
                     'inventory': {'control_plane': [{'id': target_id, 'resource_id': resource_id,
                                   'private_ipv4': matches[0], 'ssh': {**ssh, 'port': ssh.get('port', 22)}}], 'workers': []},
                     'timeout_seconds': timeout_seconds})


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError('Duplicate JSON object key')
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument('--request', default='-', help='JSON file or - for stdin')
    inputs.add_argument('--node-descriptor', help='Terraform output -json node_descriptor file')
    parser.add_argument('--request-id')
    parser.add_argument('--operation', choices=('guest.check', 'runtime.install'), default='runtime.install')
    parser.add_argument('--ssh-user')
    parser.add_argument('--identity-file')
    parser.add_argument('--known-hosts-file')
    parser.add_argument('--timeout-seconds', type=int, default=1200)
    parser.add_argument('--state-dir', type=Path, default=STATE_DIR)
    parser.add_argument('--validate-only', action='store_true', help='No SSH or file-reference checks; never marks readiness')
    args = parser.parse_args(argv)
    try:
        source = args.node_descriptor or args.request
        if source == '-':
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        else:
            with open(source, 'rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ContractError('Request exceeds 1 MiB')
        request = json.loads(raw, object_pairs_hook=unique_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ContractError('Non-finite JSON number')))
        if args.node_descriptor:
            request = from_descriptor(request, request_id=args.request_id, operation=args.operation,
                                      ssh={'user': args.ssh_user, 'identity_file': args.identity_file,
                                           'known_hosts_file': args.known_hosts_file}, timeout_seconds=args.timeout_seconds)
        result = run(request, validate_only=args.validate_only, state_dir=args.state_dir)
    except (OSError, ValueError, UnicodeError):
        result = fail(base_result(), 'invalid', 'INVALID_JSON', 'Unreadable, duplicate-key or invalid JSON request')
    print(json.dumps(result, separators=(',', ':')))
    return {'succeeded': 0, 'validated': 0, 'invalid': 2, 'blocked': 3, 'failed': 4}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())
