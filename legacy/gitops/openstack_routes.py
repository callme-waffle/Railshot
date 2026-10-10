"""Run the fixed Octavia route worker through the registered private SSH path."""
import ipaddress
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile

from edge import read_private
from edge import digest
from handoff import require
import openstack_route_worker as worker

COMMAND = 'sudo -n /usr/bin/python3 /opt/railshot/octavia/openstack_route_worker.py'
PRIVATE = tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


class RouteError(ValueError):
    def __init__(self, *, unknown):
        self.unknown = unknown
        self.code = 'OPENSTACK_ROUTE_OUTCOME_UNKNOWN' if unknown else 'OPENSTACK_ROUTE_BLOCKED'
        super().__init__(self.code)


def checked_edge(config):
    keys = {'version', 'provider', 'base_domain', 'runtime_private_address', 'controller', 'proxy'}
    require(isinstance(config, dict) and set(config) in (keys, keys | {'worker_binding'})
            and type(config['version']) is int and config['version'] == 1 and config['provider'] == 'openstack',
            'OPENSTACK_ROUTE_CONFIGURATION_INVALID')
    if 'worker_binding' in config:
        worker.checked_worker_binding(config['worker_binding'])
    return config


def rpc_command(config):
    controller, proxy = config['controller'], config['proxy']
    proxy_command = [*ssh_prefix(proxy), '-W', '%h:%p', proxy['user'] + '@' + proxy['host']]
    return [*ssh_prefix(controller), '-o', 'ProxyCommand=' + shlex.join(proxy_command),
            controller['user'] + '@' + controller['host'], COMMAND]


def registration_rpc(config, payload, status):
    try:
        result = subprocess.run(rpc_command(config), input=json.dumps(payload), capture_output=True,
                                text=True, shell=False, timeout=600)
        require(len(result.stdout) <= 65536, 'OPENSTACK_REGISTRATION_RESPONSE_INVALID')
        receipt = json.loads(result.stdout)
        require(result.returncode == 0 and isinstance(receipt, dict)
                and set(receipt) == {'status', 'https_verified', 'binding'}
                and receipt['status'] == status and receipt['https_verified'] is False
                and receipt['binding'] == payload['binding'], 'OPENSTACK_REGISTRATION_RESPONSE_INVALID')
        worker.checked_worker_binding(receipt['binding'])
        return receipt
    except (OSError, subprocess.TimeoutExpired, ValueError):
        # A lost response is retried with the same immutable binding only.
        raise RouteError(unknown=payload['operation'] != 'verify-runtime') from None


def register_runtime(edge_template, *, environment_id, generation, base, runtime):
    config = checked_edge(edge_template)
    base = worker.operator_base(base)
    require(base['base_domain'] == config['base_domain'] and runtime['private_address'] == config['runtime_private_address'],
            'OPENSTACK_REGISTRATION_BINDING_INVALID')
    binding = worker.registration_binding(base, environment_id, generation, runtime)
    require('worker_binding' not in config or config['worker_binding'] == binding,
            'OPENSTACK_REGISTRATION_BINDING_INVALID')
    return registration_rpc(config, {'operation': 'register-runtime', 'binding': binding, 'runtime': runtime},
                            'registered')['binding']


def verify_runtime(edge_template, *, environment_id, generation, base, runtime):
    config = checked_edge(edge_template)
    base = worker.operator_base(base)
    require(base['base_domain'] == config['base_domain'] and runtime['private_address'] == config['runtime_private_address'],
            'OPENSTACK_REGISTRATION_BINDING_INVALID')
    binding = worker.registration_binding(base, environment_id, generation, runtime)
    return registration_rpc(config, {'operation': 'verify-runtime', 'binding': binding, 'runtime': runtime},
                            'verified')['binding']


def unregister_runtime(edge_config):
    config = checked_edge(edge_config)
    require('worker_binding' in config, 'OPENSTACK_WORKER_BINDING_REQUIRED')
    return registration_rpc(config, {'operation': 'unregister-runtime', 'binding': config['worker_binding']}, 'unregistered')


def ssh_prefix(connection):
    require(isinstance(connection, dict) and set(connection) == {
        'host', 'port', 'user', 'identity_file', 'known_hosts_file', 'host_key_alias'},
        'OPENSTACK_SSH_CONFIGURATION_INVALID')
    for key in ('host', 'host_key_alias'):
        address = ipaddress.IPv4Address(connection[key])
        require(str(address) == connection[key] and any(address in network for network in PRIVATE),
                'OPENSTACK_PRIVATE_SSH_REQUIRED')
    require(isinstance(connection['user'], str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', connection['user'])
            and connection['user'] != 'root' and type(connection['port']) is int
            and 1 <= connection['port'] <= 65535, 'OPENSTACK_SSH_CONFIGURATION_INVALID')
    for key in ('identity_file', 'known_hosts_file'):
        path = Path(connection[key])
        info = path.lstat()
        require(path.is_absolute() and path.resolve() == path and stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                'OPENSTACK_PRIVATE_SSH_FILE_REQUIRED')
    return ['ssh', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'IdentityAgent=none', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'UserKnownHostsFile=' + connection['known_hosts_file'],
            '-o', 'HostKeyAlias=' + connection['host_key_alias'], '-o', 'ConnectTimeout=15',
            '-i', connection['identity_file'], '-p', str(connection['port'])]


def ensure(config_path, request):
    config = checked_edge(read_private(config_path))
    worker.checked_request(config, request)
    command = rpc_command(config)
    payload = ({'operation': 'environment-route', 'binding': config['worker_binding'], 'request': request}
               if 'worker_binding' in config else request)
    try:
        result = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True,
                                shell=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        raise RouteError(unknown=True) from None
    if len(result.stdout) > 65536:
        raise RouteError(unknown=True)
    try:
        receipt = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise RouteError(unknown=True) from None
    if result.returncode != 0:
        # The worker can report blocked after creating a pool or firewall rule.
        # Its failure status cannot prove the remote operation made no changes.
        raise RouteError(unknown=True)
    if not (isinstance(receipt, dict) and receipt.get('status') == 'configured'
            and receipt.get('https_verified') is False
            and all(receipt.get(key) == value for key, value in request.items())
            and isinstance(receipt.get('resources'), dict)):
        raise RouteError(unknown=True)
    if not (set(receipt['resources']) == {'pool', 'member', 'monitor', 'policy', 'rule'}
            and all(isinstance(value, str) and re.fullmatch(
                r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value)
                    for value in receipt['resources'].values())):
        raise RouteError(unknown=True)
    if not (isinstance(receipt.get('network_rule_id'), str) and re.fullmatch(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', receipt['network_rule_id'])):
        raise RouteError(unknown=True)
    return receipt


def tunnel_module():
    spec = importlib.util.spec_from_file_location('railshot_lifecycle_tunnel',
        Path(__file__).resolve().parents[1] / 'deployment/cloudflared/register.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lifecycle_request(binding):
    return {'application_id': binding['application_id'], 'hostname': binding['hostname'],
            'node_port': binding['registered']['target']['node_port']}


def tunnel_request(binding):
    return {'application_id': binding['application_id'], 'hostname': binding['hostname'],
            'environment_id': binding['environment_id'], 'app': binding['registered']['app'],
            'tenant': binding['registered']['tenant']}


def lifecycle_config(binding):
    config = read_private(binding['ingress']['tunnel_config_file'])
    require(config.get('environment_id') == binding['environment_id'] and
            isinstance(config.get('state_dir'), str) and Path(config['state_dir']).is_absolute(),
            'OPENSTACK_LIFECYCLE_AUTHORITY_INVALID')
    return {'state_dir': config['state_dir']}


def lifecycle_rpc(binding, action, operation, expected=None):
    config = checked_edge(read_private(binding['ingress']['edge_config_file']))
    request = lifecycle_request(binding)
    worker.checked_request(config, {**request, 'private_address': config['runtime_private_address'], 'health_path': '/'})
    command = rpc_command(config)
    payload = {'operation': operation, 'action': action, 'request': request}
    if expected is not None:
        payload['expected'] = expected
    if 'worker_binding' in config:
        payload = {'operation': 'environment-lifecycle', 'binding': config['worker_binding'], 'request': payload}
    try:
        result = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True,
                                shell=False, timeout=600)
        require(len(result.stdout) <= 65536, 'OPENSTACK_LIFECYCLE_RESPONSE_INVALID')
        receipt = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise RouteError(unknown=operation == 'lifecycle-execute') from None
    if result.returncode != 0 or not isinstance(receipt, dict) or receipt.get('status') != 'succeeded' or receipt.get('https_verified') is not False:
        raise RouteError(unknown=operation == 'lifecycle-execute')
    return receipt


def lifecycle_plan(binding, action):
    tunnel = tunnel_module()
    connector = tunnel.lifecycle_plan(binding['ingress']['tunnel_config_file'], tunnel_request(binding), action)
    edge_config = read_private(binding['ingress']['edge_config_file'])
    require(edge_config['runtime_private_address'] == connector['config']['runtime_private_address'] and
            edge_config['base_domain'] == connector['config']['base_domain'] == binding['ingress']['base_domain'],
            'OPENSTACK_LIFECYCLE_RUNTIME_MISMATCH')
    result = lifecycle_rpc(binding, action, 'lifecycle-plan')
    expected = result.get('expected', {})
    require(set(expected) == {'plan_id', 'plan_sha256'} and worker.is_uuid(expected['plan_id']) and
            isinstance(expected['plan_sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', expected['plan_sha256']),
            'OPENSTACK_LIFECYCLE_PLAN_INVALID')
    resources = result.get('resources')
    require(isinstance(resources, dict) and (not resources or set(resources) == set(worker.KINDS)) and
            all(worker.is_uuid(value) for value in resources.values()), 'OPENSTACK_LIFECYCLE_RESOURCES_INVALID')
    changes = [{'address': 'openstack_' + kind + '.app', 'actions': ['create' if action == 'start' else 'delete']}
               for kind in (worker.KINDS if action == 'start' else resources)]
    if resources or action == 'start':
        changes.append({'address': 'openstack_security_group_rule.app', 'actions': ['create' if action == 'start' else 'delete']})
    changes.append({'address': 'cloudflared_ingress.app', 'actions': ['update']})
    root = lifecycle_config(binding)['state_dir']
    work = tempfile.mkdtemp(prefix='application-lifecycle-', dir=root)
    return {'work': work, 'changes': changes, 'worker': expected, 'tunnel': connector,
            'edge_config_sha256': digest(edge_config), 'health_path': result.get('health_path')}


def lifecycle_validate(binding, action, expected):
    require(digest(read_private(binding['ingress']['edge_config_file'])) == expected['edge_config_sha256'],
            'OPENSTACK_LIFECYCLE_AUTHORITY_CHANGED')
    tunnel_module().lifecycle_validate(binding['ingress']['tunnel_config_file'], tunnel_request(binding), action, expected['tunnel'])
    lifecycle_rpc(binding, action, 'lifecycle-validate', expected['worker'])


def lifecycle_execute(binding, action, expected):
    lifecycle_validate(binding, action, expected)
    worker_result = lifecycle_rpc(binding, action, 'lifecycle-execute', expected['worker'])
    connector = tunnel_module().lifecycle_execute(binding['ingress']['tunnel_config_file'], tunnel_request(binding), action, expected['tunnel'])
    require(connector.get('status') == 'succeeded' and connector.get('route_present') is (action == 'start'),
            'OPENSTACK_LIFECYCLE_TUNNEL_UNVERIFIED')
    return {'worker': worker_result, 'tunnel': connector}
