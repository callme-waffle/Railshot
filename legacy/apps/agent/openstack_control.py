#!/usr/bin/env python3
"""Project-bound structured OpenStack CLI, invoked only by a fixed SSH command."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'deployment/bootstrap'))
from apps.agent.protocol import MAX_REQUEST_BYTES, ProtocolError, _unique_object, success_response, error_response
from apps.agent.runner import _read_bounded
from client_setup.credentials import CredentialStore
from client_setup.state import private_directory, read_private, atomic_private_write
from infrastructure.providers.openstack.cli import OpenStackCLI, ProviderError

COMMAND = 'railshot-openstack-v1'
HOME = Path('/var/lib/railshot-personal-cli')
RESOURCES = ('security group rule', 'security group', 'floating ip', 'availability zone',
             'server', 'volume', 'network', 'subnet', 'router', 'port', 'image', 'flavor', 'keypair', 'limits', 'quota')
CREATES = {
    'server': {'--image', '--flavor', '--network', '--nic', '--key-name', '--security-group', '--property'},
    'volume': {'--size', '--type', '--description', '--availability-zone', '--image', '--property'},
    'network': {'--description', '--mtu'},
    'subnet': {'--network', '--subnet-range', '--gateway', '--dns-nameserver', '--allocation-pool', '--ip-version'},
    'router': {'--description'},
    'port': {'--network', '--fixed-ip', '--security-group', '--description'},
    'floating ip': {'--description', '--port', '--fixed-ip-address'},
    'security group': {'--description'},
    'security group rule': {'--ingress', '--egress', '--ethertype', '--protocol', '--dst-port', '--remote-ip', '--remote-group'},
    'keypair': set(),
}
BOOLEAN_FLAGS = {'--ingress', '--egress'}
FIELDS = {'id', 'name', 'status', 'project_id', 'tenant_id', 'user_id', 'addresses', 'networks', 'image', 'flavor',
          'size', 'volume_type', 'availability_zone', 'availability_zone_state', 'zone_name', 'zone_state',
          'network_id', 'subnet_id', 'subnets', 'cidr', 'gateway_ip', 'ip_version', 'allocation_pools',
          'dns_nameservers', 'fixed_ips', 'fixed_ip_addresses', 'mac_address', 'device_id', 'device_owner', 'admin_state_up',
          'router_id', 'port_id', 'floating_ip_address', 'fixed_ip_address', 'floating_network_id',
          'security_groups', 'security_group_ids', 'security_group_id', 'security_group_rules', 'direction', 'ethertype',
          'protocol', 'port_range_min', 'port_range_max', 'remote_ip_prefix', 'remote_group_id',
          'public_key', 'fingerprint', 'type', 'ram', 'vcpus', 'disk', 'ephemeral', 'swap',
          'min_disk', 'min_ram', 'visibility', 'protected', 'shared', 'is_public', 'mtu',
          'attachments', 'bootable', 'encrypted', 'multiattach', 'created_at', 'updated_at',
          'volumes_attached', 'os_extended_volumes:volumes_attached', 'value', 'cores', 'instances', 'gigabytes',
          'volumes', 'snapshots', 'floating_ips', 'fixed_ips', 'security_groups', 'security_group_rules',
          'injected_files', 'injected_file_content_bytes', 'injected_file_path_bytes', 'key_pairs',
          'server_groups', 'server_group_members', 'properties'}
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')


def require(value):
    if not value:
        raise ProtocolError('command_rejected')


def decode(raw):
    require(isinstance(raw, bytes) and len(raw) <= MAX_REQUEST_BYTES)
    try:
        request = json.loads(raw, object_pairs_hook=_unique_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError('invalid_request') from None
    require(isinstance(request, dict) and set(request) == {'version', 'job_id', 'action', 'params'})
    require(type(request['version']) is int and request['version'] == 1 and request['action'] == 'openstack.execute')
    require(isinstance(request['job_id'], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request['job_id']))
    params = request['params']
    require(isinstance(params, dict) and 'argv' in params and set(params) <= {'argv', 'delete_data', 'public_key'})
    require('delete_data' not in params or type(params['delete_data']) is bool)
    parse(params['argv'])
    return request


def parse(argv):
    require(isinstance(argv, list) and 2 <= len(argv) <= 64)
    require(all(isinstance(arg, str) and 0 < len(arg) <= 1024 and not any(ord(c) < 32 or ord(c) == 127 for c in arg) for arg in argv))
    resource = next((r for r in RESOURCES if argv[:len(r.split())] == r.split()), None)
    require(resource is not None)
    offset = len(resource.split())
    require(len(argv) > offset)
    verb = argv[offset]
    require(verb in ('list', 'show', 'create', 'delete'))
    rest = argv[offset + 1:]
    if verb == 'list':
        require(resource not in ('quota', 'limits') and (not rest or resource == 'security group rule' and len(rest) == 1 and IDENT.fullmatch(rest[0])))
    elif verb == 'show':
        require(resource != 'availability zone' and (not rest if resource in ('quota', 'limits') else len(rest) == 1 and IDENT.fullmatch(rest[0])))
    elif verb == 'delete':
        require(resource in CREATES and len(rest) == 1 and IDENT.fullmatch(rest[0]))
    else:
        require(resource in CREATES)
        positional = []
        index = 0
        while index < len(rest):
            argument = rest[index]
            if argument.startswith('-'):
                require(argument in CREATES[resource])  # no equals/abbreviations/global flags
                if argument not in BOOLEAN_FLAGS:
                    index += 1
                    require(index < len(rest) and not rest[index].startswith('-'))
            else:
                require(IDENT.fullmatch(argument))
                positional.append(argument)
            index += 1
        require(len(positional) == 1 and positional[0] == rest[-1])
    return resource, verb


def sanitize(result, secrets):
    if isinstance(result, list):
        require(len(result) <= 10000)
        return [sanitize(row, secrets) for row in result]
    require(isinstance(result, dict))
    filtered = {name: value for name, value in result.items() if name.lower().replace(' ', '_') in FIELDS}
    raw = json.dumps(filtered, allow_nan=False)
    require(not any(value and value in raw for value in secrets))
    # Deny nested credential-looking fields even inside otherwise useful provider structures.
    def clean(value):
        if isinstance(value, str):
            require(not any(secret and secret in value for secret in secrets))
        if isinstance(value, dict):
            require(not any(secret and secret in key for key in value for secret in secrets))
            return {k: clean(v) for k, v in value.items() if not re.search(r'password|secret|token|private.?key|user.?data', k, re.I)}
        if isinstance(value, list):
            return [clean(v) for v in value]
        require(value is None or isinstance(value, (str, int, float, bool)))
        return value
    return clean(filtered)


def load(path, default):
    return json.loads(read_private(path)) if path.exists() else default


def save(path, value):
    atomic_private_write(path, json.dumps(value, sort_keys=True).encode())


def require_project(cli, resource, reference, project_id, shared=False):
    require(isinstance(reference, str) and IDENT.fullmatch(reference))
    observed = cli.run(resource.split() + ['show', reference])
    project = observed.get('project_id', observed.get('tenant_id'))
    require(project == project_id or shared and observed.get('shared') is True)
    return observed


def validate_references(cli, resource, argv, project_id):
    # A project-scoped token can still carry administrator roles. Verify project
    # ownership of every supplied resource reference before a mutating command.
    references = {'--network': 'network', '--port': 'port', '--security-group': 'security group',
                  '--remote-group': 'security group'}
    for index, flag in enumerate(argv):
        if flag in references:
            require_project(cli, references[flag], argv[index + 1], project_id,
                            shared=flag == '--network' and resource in ('server', 'port'))
        if flag == '--nic':
            parts = dict(item.split('=', 1) for item in argv[index + 1].split(','))
            require(set(parts) <= {'net-id', 'port-id', 'v4-fixed-ip', 'v6-fixed-ip'}
                    and len(set(parts) & {'net-id', 'port-id'}) == 1)
            if 'net-id' in parts:
                require_project(cli, 'network', parts['net-id'], project_id, shared=True)
            if 'port-id' in parts:
                require_project(cli, 'port', parts['port-id'], project_id)
    if resource == 'security group rule':
        require_project(cli, 'security group', argv[-1], project_id)
    if resource == 'floating ip':
        # Floating-IP pools are normally shared external provider networks.
        observed = cli.run(['network', 'show', argv[-1]])
        require(observed.get('project_id', observed.get('tenant_id')) == project_id
                or observed.get('router:external') is True)


def execute(request, config, cli, home=HOME, secrets=()):
    argv = request['params']['argv']
    resource, verb = parse(argv)
    require(IDENT.fullmatch(config['project_id']) and IDENT.fullmatch(config['target_id']))
    # Even valid credentials must remain scoped to the enrolled project. Tokens
    # are used here only and never emitted in output, job history, or logs.
    token = cli.run(['token', 'issue'])
    require(token.get('project_id') == config['project_id'])
    secrets = (*secrets, token.get('id', ''))
    if verb in ('list', 'show'):
        scoped = list(argv)
        if verb == 'list' and resource in ('network', 'subnet', 'router', 'port', 'floating ip', 'security group', 'security group rule'):
            scoped += ['--project', config['project_id']]
        if resource == 'limits':
            scoped += ['--absolute']
        result = cli.run(scoped)
        if isinstance(result, dict):
            project = result.get('project_id', result.get('tenant_id'))
            require(project == config['project_id'] or resource in ('image', 'flavor', 'keypair', 'limits', 'quota')
                    or resource == 'network' and result.get('shared') is True)
        return sanitize(result, secrets)
    private_directory(home)
    lock_fd = os.open(home / 'jobs.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        job_path = home / ('job-' + request['job_id'] + '.json')
        prior = load(job_path, None)
        if prior:
            require(prior['fingerprint'] == fingerprint)
            if prior['status'] != 'succeeded':
                raise ProviderError('mutation_unknown')
            return prior['result']
        owned_path = home / 'resources.json'
        owned = load(owned_path, {})
        if verb == 'delete':
            ident = resource + ':' + argv[-1]
            record = owned.get(ident)
            require(record and record['target_id'] == config['target_id'] and not record.get('removed'))
            if resource in ('server', 'volume'):
                require(request['params'].get('delete_data') is True)
            observed = cli.run(resource.split() + ['show', argv[-1]])
            project = observed.get('project_id', observed.get('tenant_id'))
            require(resource == 'keypair' or project == config['project_id'])
            if resource == 'keypair':
                require(record.get('identity') == keypair_identity(observed))
        if resource == 'keypair' and verb == 'create':
            from apps.agent.install_forced_command import authorized_key_line
            public = request['params'].get('public_key', '')
            authorized_key_line(public, '127.0.0.1', '/usr/bin/python3', '/opt/railshot/personal', '/etc/railshot-personal')
        else:
            require('public_key' not in request['params'])
        if verb == 'create':
            validate_references(cli, resource, argv, config['project_id'])
        # Persist intent before any provider mutation. Interrupted/failed jobs are
        # not automatically replayed, even if the caller retries after timeout.
        save(job_path, {'fingerprint': fingerprint, 'status': 'unknown'})
        if verb == 'create':
            if resource == 'keypair':
                with tempfile.TemporaryDirectory(prefix='railshot-public-key-') as folder:
                    path = Path(folder) / 'public.key'
                    path.write_text(request['params']['public_key'])
                    result = cli.run([*argv[:-1], '--public-key', str(path), argv[-1]])
            else:
                result = cli.run(argv)
            resource_id = result.get('id', result.get('ID', result.get('name', result.get('Name')) if resource == 'keypair' else None))
            require(isinstance(resource_id, str) and IDENT.fullmatch(resource_id))
            owned[resource + ':' + resource_id] = {'target_id': config['target_id'], 'project_id': config['project_id'], 'removed': False,
                **({'identity': keypair_identity(result)} if resource == 'keypair' else {})}
            save(owned_path, owned)
            result = sanitize(result, secrets)
        else:
            cli.run(argv, json_output=False)
            try:
                cli.run(resource.split() + ['show', argv[-1]])
            except ProviderError as exc:
                require(exc.code == 'not_found')
            else:
                raise ProviderError('mutation_unknown')
            owned[ident]['removed'] = True
            save(owned_path, owned)
            result = {'id': argv[-1], 'status': 'deleted'}
        save(job_path, {'fingerprint': fingerprint, 'status': 'succeeded', 'result': result})
        return result
    finally:
        os.close(lock_fd)


def keypair_identity(value):
    normalized = {key.lower(): entry for key, entry in value.items()}
    fingerprint = normalized.get('fingerprint')
    public_key = normalized.get('public_key')
    require(isinstance(fingerprint, str) and fingerprint)
    require(isinstance(public_key, str) and public_key)
    return hashlib.sha256(json.dumps([fingerprint, ' '.join(public_key.split()[:2])]).encode()).hexdigest()


def execute_raw(raw, original_command, credential_loader, config, cli_factory=OpenStackCLI, home=HOME):
    request = None
    try:
        require(original_command == COMMAND)
        request = decode(raw)
        auth = credential_loader()
        require(isinstance(auth, dict))
        secrets = tuple(v for k, v in auth.items() if any(word in k for word in ('secret', 'password', 'token')) and isinstance(v, str))
        result = execute(request, config, cli_factory(auth), home, secrets)
        return success_response(request, result)
    except Exception as exc:
        unknown = False
        if request:
            try:
                prior = load(home / ('job-' + request['job_id'] + '.json'), None)
                unknown = prior is not None and prior['status'] == 'unknown' and prior['fingerprint'] == hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            except Exception:
                pass
        response = error_response(exc.code if isinstance(exc, ProtocolError) else 'execution_failed', request)
        if unknown:
            response['error']['code'] = 'execution_unknown'
        return response


def main():
    # Arguments and environment cannot select a different credential store.
    if os.geteuid() == 0 or len(sys.argv) != 1:
        print(json.dumps(error_response('command_rejected')))
        return 1
    try:
        original = os.environ.get('SSH_ORIGINAL_COMMAND', '')
        os.environ.clear()
        os.environ.update(PATH='/usr/bin:/bin', HOME=str(HOME), LANG='C.UTF-8')
        config = load(HOME / 'control.json', {})
        response = execute_raw(_read_bounded(sys.stdin.buffer), original,
                               lambda: CredentialStore(HOME).load(), config)
    except Exception:
        response = error_response('execution_failed')
    print(json.dumps(response))
    return 0 if response['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
