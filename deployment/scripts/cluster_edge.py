#!/usr/bin/env python3
"""Provision a cluster-owned native L7 edge for the kubeadm 1-control/2-compute profile.

All authority, variables, Terraform state and DNS receipts remain private. This
executor creates only an empty isolated stage, never adopts a live edge state,
never destroys resources and never replays an uncertain mutation. Configuration
readback is deliberately separate from backend, DNS, TLS and application probes.
"""
from contextlib import contextmanager
import copy
import base64
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'gitops'), str(ROOT / 'ci/scripts')]
from storage import durable_write
import dns

PROVIDERS = {'aws': 'aws_', 'gcp': 'google_', 'openstack': 'openstack_'}
EDGE_KINDS = {
    'aws': {'aws_security_group', 'aws_security_group_rule', 'aws_lb', 'aws_lb_target_group',
            'aws_lb_target_group_attachment', 'aws_acm_certificate', 'aws_route53_record',
            'aws_acm_certificate_validation', 'aws_lb_listener', 'aws_lb_listener_certificate', 'aws_lb_listener_rule'},
    'gcp': {'google_project_service', 'google_compute_firewall', 'google_compute_network_endpoint_group',
            'google_compute_network_endpoint', 'google_compute_health_check', 'google_compute_backend_service',
            'google_compute_url_map', 'google_certificate_manager_dns_authorization', 'google_certificate_manager_certificate',
            'google_certificate_manager_certificate_map', 'google_certificate_manager_certificate_map_entry',
            'google_compute_target_https_proxy', 'google_compute_target_http_proxy', 'google_compute_global_address',
            'google_compute_global_forwarding_rule'},
    'openstack': {'openstack_lb_loadbalancer_v2', 'openstack_lb_listener_v2', 'openstack_lb_pool_v2',
                  'openstack_lb_member_v2', 'openstack_lb_monitor_v2', 'openstack_lb_l7policy_v2', 'openstack_lb_l7rule_v2'},
}
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
FQDN = r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}'


class EdgeError(ValueError):
    def __init__(self, code, *, unknown=False):
        self.code, self.unknown = code, unknown
        super().__init__(code)


def require(value, code):
    if not value:
        raise EdgeError(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def pairs(items):
    value = {}
    for key, child in items:
        require(key not in value, 'CLUSTER_EDGE_DUPLICATE_JSON_KEY')
        value[key] = child
    return value


def private_json(path):
    try:
        path = Path(path)
        require(path.is_absolute() and path.resolve() == path, 'CLUSTER_EDGE_PRIVATE_FILE_REQUIRED')
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                    not info.st_mode & 0o077 and 0 < info.st_size <= 2_000_000,
                    'CLUSTER_EDGE_PRIVATE_FILE_REQUIRED')
            return json.loads(stream.read(2_000_001), object_pairs_hook=pairs)
    except EdgeError:
        raise
    except (OSError, TypeError, ValueError):
        raise EdgeError('CLUSTER_EDGE_PRIVATE_FILE_REQUIRED') from None


def private_directory(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path, 'CLUSTER_EDGE_PRIVATE_STATE_REQUIRED')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            'CLUSTER_EDGE_PRIVATE_STATE_REQUIRED')
    return path


def private_ip(value):
    try:
        ip = ipaddress.IPv4Address(value)
        return str(ip) == value and any(ip in ipaddress.ip_network(cidr) for cidr in
            ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    except (ValueError, TypeError):
        return False


def path_valid(value):
    return isinstance(value, str) and len(value) <= 200 and re.fullmatch(r'/([A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*)?', value)


def cloud_auth(config):
    """Validate explicit private provider credentials without returning them in a receipt."""
    credential = private_json(config['credentials_file'])
    require(isinstance(credential, dict), 'CLUSTER_EDGE_CREDENTIAL_INVALID')
    if config['provider'] == 'aws':
        require({'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'} <= set(credential) <=
                {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'} and
                all(isinstance(value, str) and value and '\x00' not in value for value in credential.values()),
                'CLUSTER_EDGE_CREDENTIAL_INVALID')
    elif config['provider'] == 'gcp':
        require(credential.get('type') == 'service_account' and credential.get('project_id') == config['account_id'] and
                isinstance(credential.get('client_email'), str) and
                re.fullmatch(r'[a-z0-9-]+@' + re.escape(config['account_id']) + r'\.iam\.gserviceaccount\.com', credential['client_email']) and
                isinstance(credential.get('private_key'), str) and '-----BEGIN PRIVATE KEY-----' in credential['private_key'] and
                credential.get('token_uri') == 'https://oauth2.googleapis.com/token', 'CLUSTER_EDGE_CREDENTIAL_INVALID')
    else:
        allowed = {'OS_AUTH_URL', 'OS_PROJECT_ID', 'OS_REGION_NAME', 'OS_USERNAME', 'OS_PASSWORD',
                   'OS_USER_DOMAIN_NAME', 'OS_USER_DOMAIN_ID', 'OS_APPLICATION_CREDENTIAL_ID',
                   'OS_APPLICATION_CREDENTIAL_SECRET', 'OS_AUTH_TYPE', 'OS_INTERFACE', 'OS_CACERT'}
        require({'OS_AUTH_URL', 'OS_PROJECT_ID'} <= set(credential) <= allowed and
                all(isinstance(value, str) and value and '\x00' not in value for value in credential.values()) and
                credential['OS_PROJECT_ID'] == config['account_id'] and
                credential.get('OS_REGION_NAME', config['region']) == config['region'] and
                credential.get('OS_INTERFACE', 'public') in ('public', 'internal', 'admin'), 'CLUSTER_EDGE_CREDENTIAL_INVALID')
        url = urlsplit(credential['OS_AUTH_URL'])
        require(url.scheme == 'https' and url.hostname and not url.username and not url.password and
                not url.query and not url.fragment, 'CLUSTER_EDGE_CREDENTIAL_INVALID')
        application = {'OS_APPLICATION_CREDENTIAL_ID', 'OS_APPLICATION_CREDENTIAL_SECRET'}
        if application & set(credential):
            require(application <= set(credential) and not {'OS_USERNAME', 'OS_PASSWORD'} & set(credential) and
                    credential.get('OS_AUTH_TYPE', 'v3applicationcredential') == 'v3applicationcredential', 'CLUSTER_EDGE_CREDENTIAL_INVALID')
        else:
            require({'OS_USERNAME', 'OS_PASSWORD'} <= set(credential) and
                    bool({'OS_USER_DOMAIN_ID', 'OS_USER_DOMAIN_NAME'} & set(credential)) and
                    credential.get('OS_AUTH_TYPE', 'v3password') in ('password', 'v3password'), 'CLUSTER_EDGE_CREDENTIAL_INVALID')
        if credential.get('OS_CACERT'):
            ca = Path(credential['OS_CACERT'])
            info = ca.lstat()
            require(ca.is_absolute() and ca.resolve() == ca and stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                    not info.st_mode & 0o077 and 0 < info.st_size <= 1_000_000, 'CLUSTER_EDGE_CREDENTIAL_INVALID')
    return credential


def credential_env(config, original):
    """Provider SDK/CLI auth shares one private file; ambient auth is not an authority."""
    env = {key: original[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'TF_PLUGIN_CACHE_DIR') if key in original}
    credential = cloud_auth(config)
    if config['provider'] == 'aws':
        env.update(credential)
        env.update(AWS_REGION=config['region'], AWS_DEFAULT_REGION=config['region'], AWS_PAGER='',
                   AWS_CONFIG_FILE='/dev/null', AWS_SHARED_CREDENTIALS_FILE='/dev/null', AWS_EC2_METADATA_DISABLED='true')
    elif config['provider'] == 'gcp':
        env.update(GOOGLE_APPLICATION_CREDENTIALS=config['credentials_file'], CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=config['credentials_file'],
                   CLOUDSDK_CORE_PROJECT=config['account_id'])
    else:
        env.update(credential)
        env.update(OS_PROJECT_ID=config['account_id'], OS_REGION_NAME=config['region'],
                   OS_AUTH_TYPE=credential.get('OS_AUTH_TYPE', 'v3applicationcredential' if 'OS_APPLICATION_CREDENTIAL_ID' in credential else 'v3password'),
                   OS_IDENTITY_API_VERSION='3', OS_INTERFACE=credential.get('OS_INTERFACE', 'public'))
    env['TF_IN_AUTOMATION'] = '1'
    return env


def validate(request):
    """Bind private provider authority to the core's validated 3-node inventory."""
    target, inventory = request['target'], request['inventory']
    require(request['runtime'].get('profile') == 'kubeadm-1c2w', 'CLUSTER_EDGE_PROFILE_REQUIRED')
    config = private_json(request['runtime']['edge'])
    fields = {'version', 'cluster_id', 'provider', 'account_id', 'region', 'variables_file', 'worker_bindings', 'credentials_file'}
    require(isinstance(config, dict) and fields <= set(config) <= fields |
            {'dns_config_file', 'tunnel', 'timeout_seconds', 'aws_authority_file', 'gcp_authority_file'} and type(config['version']) is int and config['version'] == 1,
            'CLUSTER_EDGE_CONFIG_INVALID')
    require(config['provider'] in PROVIDERS and
            (config['cluster_id'], config['provider'], config['account_id'], config['region']) ==
            (target['id'], target['provider'], target['account_id'], target['placement']), 'CLUSTER_EDGE_SCOPE_MISMATCH')
    require(isinstance(target['id'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', target['id']), 'CLUSTER_EDGE_ID_INVALID')
    controls, workers = inventory['control_plane'], inventory['workers']
    require(len(controls) == 1 and len(workers) == 2, 'CLUSTER_EDGE_TOPOLOGY_INVALID')
    nodes = controls + workers
    require(all(isinstance(node.get('id'), str) and re.fullmatch(r'[a-z][a-z0-9-]{0,62}', node['id']) and
                isinstance(node.get('resource_id'), str) and private_ip(node.get('private_ipv4')) and
                isinstance(node.get('availability_zone'), str) and node['availability_zone'] and
                node.get('provider', target['provider']) == target['provider'] and
                node.get('account_id', target['account_id']) == target['account_id'] and
                node.get('region', target['placement']) == target['placement'] for node in nodes),
            'CLUSTER_EDGE_NODE_SCOPE_INVALID')
    require(all(len({node[key] for node in nodes}) == 3 for key in ('id', 'resource_id', 'private_ipv4')),
            'CLUSTER_EDGE_DUPLICATE_NODE')
    bindings = config['worker_bindings']
    require(isinstance(bindings, dict) and set(bindings) == {node['resource_id'] for node in workers},
            'CLUSTER_EDGE_COMPUTE_BINDING_MISMATCH')
    for node in nodes:
        identity = node['resource_id']
        if target['provider'] == 'aws':
            require(re.fullmatch('arn:aws:ec2:' + re.escape(target['placement']) + ':' + re.escape(target['account_id']) + r':instance/i-[0-9a-f]{8,17}', identity) and
                    node['availability_zone'].startswith(target['placement']) and
                    len(node['availability_zone']) == len(target['placement']) + 1, 'CLUSTER_EDGE_NODE_SCOPE_INVALID')
        elif target['provider'] == 'gcp':
            require(re.fullmatch('projects/' + re.escape(target['account_id']) + '/zones/' +
                re.escape(node['availability_zone']) + r'/instances/[a-z][a-z0-9-]{0,61}[a-z0-9]', identity) and
                node['availability_zone'].startswith(target['placement'] + '-'), 'CLUSTER_EDGE_NODE_SCOPE_INVALID')
        else:
            require(re.fullmatch(UUID, identity), 'CLUSTER_EDGE_NODE_SCOPE_INVALID')
    values = private_json(config['variables_file'])
    require(isinstance(values, dict), 'CLUSTER_EDGE_VARIABLES_INVALID')
    module = ROOT / 'infrastructure/terraform' / (config['provider'] + '-edge')
    allowed = set(re.findall(r'variable\s+"([a-z0-9_]+)"', (module / 'variables.tf').read_text()))
    require(set(values) <= allowed and 'compute_nodes' not in values, 'CLUSTER_EDGE_VARIABLES_INVALID')
    for node in workers:
        binding = bindings[node['resource_id']]
        fields = {'aws': {'security_group_id'}, 'gcp': set(), 'openstack': {'subnet_id'}}[target['provider']]
        require(isinstance(binding, dict) and set(binding) == fields, 'CLUSTER_EDGE_COMPUTE_BINDING_MISMATCH')
        if target['provider'] == 'aws':
            require(re.fullmatch(r'sg-[0-9a-f]{8,17}', binding['security_group_id']), 'CLUSTER_EDGE_COMPUTE_BINDING_MISMATCH')
        elif target['provider'] == 'openstack':
            require(re.fullmatch(UUID, binding['subnet_id']), 'CLUSTER_EDGE_COMPUTE_BINDING_MISMATCH')
    credential = cloud_auth(config)
    authority_hashes = {'provider_credentials': digest({key: config[key] for key in ('provider', 'account_id', 'region', 'credentials_file')})}
    for provider, key in (('aws', 'account_id'), ('gcp', 'project_id')):
        if config.get(provider + '_authority_file'):
            require(config['provider'] == provider, 'CLUSTER_EDGE_CREDENTIAL_INVALID')
            authority = private_json(config[provider + '_authority_file'])
            require(authority['context']['environment_id'] == config['cluster_id'] and authority['config'][key] == config['account_id']
                    and authority['credentials_file'] == config['credentials_file'], 'CLUSTER_EDGE_CREDENTIAL_SCOPE_INVALID')
            authority_hashes['account_authority'] = digest(authority)
    if credential.get('OS_CACERT'):
        authority_hashes['provider_ca'] = hashlib.sha256(Path(credential['OS_CACERT']).read_bytes()).hexdigest()
    values = render_values(config, values, workers)
    hosts = [values['hostname']] if target['provider'] == 'gcp' else [route['host'] for route in values['routes'].values()]
    if target['provider'] == 'aws':
        base = values['base_domain']
        require(isinstance(base, str) and re.fullmatch(FQDN, base) and all(host == base or
                re.fullmatch(r'[a-z0-9-]+\.' + re.escape(base), host) for host in hosts), 'CLUSTER_EDGE_DNS_SCOPE_MISMATCH')
        # Route53 owns both ACM validation and application records in this stage.
        require('dns_config_file' not in config and 'tunnel' not in config and values.get('zone_id'),
                'CLUSTER_EDGE_DNS_AUTHORITY_INVALID')
    else:
        require(isinstance(config.get('dns_config_file'), str), 'CLUSTER_EDGE_DNS_AUTHORITY_INVALID')
        dns_config = dns.config_at(config['dns_config_file'])
        authority_hashes['dns'] = digest(dns_config)
        require(all(host.endswith('.' + dns_config['base_domain']) and
                    host.count('.') == dns_config['base_domain'].count('.') + 1 for host in hosts),
                'CLUSTER_EDGE_DNS_SCOPE_MISMATCH')
        if target['provider'] == 'openstack':
            tunnel = config.get('tunnel')
            require(isinstance(tunnel, dict) and set(tunnel) == {'config_file', 'request'} and len(hosts) == 1,
                    'CLUSTER_EDGE_TUNNEL_REQUIRED')
            tr = tunnel['request']
            require(isinstance(tr, dict) and set(tr) == {'environment_id', 'application_id', 'app', 'tenant', 'hostname'} and all(isinstance(value, str) for value in tr.values()) and re.fullmatch(r'[a-z][a-z0-9-]{1,28}[a-z0-9]', tr['app']) and re.fullmatch(r'[a-z0-9]{1,20}', tr['tenant']), 'CLUSTER_EDGE_TUNNEL_REQUEST_INVALID')
            expected_id = 'app-' + hashlib.sha256(json.dumps([tr['environment_id'], tr['tenant'], tr['app']], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:24]
            require(tr['application_id'] == expected_id, 'CLUSTER_EDGE_TUNNEL_REQUEST_INVALID')
            tunnel_config = private_json(tunnel['config_file'])
            authority_hashes['tunnel'] = digest(tunnel_config)
            common = (tunnel['request'].get('hostname') == hosts[0] and
                      tunnel['request'].get('environment_id') == target['id'] and
                      tunnel_config.get('origin_vip') == values.get('vip_address') and private_ip(values.get('vip_address')))
            require(common, 'CLUSTER_EDGE_TUNNEL_SCOPE_MISMATCH')
            if tunnel_config.get('profile') == 'kubeadm-1c2w':
                required = {'version', 'profile', 'cluster_id', 'namespace', 'name', 'tunnel_id',
                            'credentials_secret', 'credentials_file', 'origin_vip'}
                require(required <= set(tunnel_config) <= required | {'ca_configmap', 'ca_file'} and
                        tunnel_config['version'] == 1 and tunnel_config['cluster_id'] == target['id'] and
                        bool(tunnel_config.get('ca_configmap')) == bool(tunnel_config.get('ca_file')) and
                        tunnel_config['namespace'] not in ('default', 'kube-system', 'kube-public', 'kube-node-lease'),
                        'CLUSTER_EDGE_TUNNEL_SCOPE_MISMATCH')
                creds = private_json(tunnel_config['credentials_file'])
                authority_hashes['tunnel_credentials'] = digest(creds)
                require(set(creds) == {'AccountTag', 'TunnelSecret', 'TunnelID'} and
                        creds['TunnelID'] == tunnel_config['tunnel_id'] and
                        isinstance(creds['AccountTag'], str) and re.fullmatch(r'[a-f0-9]{32}', creds['AccountTag']) and
                        isinstance(creds['TunnelSecret'], str) and len(creds['TunnelSecret']) <= 256,
                        'CLUSTER_EDGE_TUNNEL_CREDENTIALS_INVALID')
                try:
                    require(len(base64.b64decode(creds['TunnelSecret'], validate=True)) == 32,
                            'CLUSTER_EDGE_TUNNEL_CREDENTIALS_INVALID')
                except ValueError:
                    raise EdgeError('CLUSTER_EDGE_TUNNEL_CREDENTIALS_INVALID') from None
                if tunnel_config.get('ca_file'):
                    ca = Path(tunnel_config['ca_file'])
                    info = ca.lstat()
                    require(ca.is_absolute() and ca.resolve() == ca and stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077 and 0 < info.st_size <= 1_000_000, 'CLUSTER_EDGE_TUNNEL_CA_INVALID')
                    authority_hashes['tunnel_ca'] = hashlib.sha256(ca.read_bytes()).hexdigest()
                tunnel_renderer().render(**{key: tunnel_config[key] for key in
                    ('namespace', 'name', 'tunnel_id', 'credentials_secret', 'origin_vip')},
                    hostnames=hosts, ca_configmap=tunnel_config.get('ca_configmap'))
            else:
                authority_hashes['tunnel_registry'] = digest(private_json(tunnel_config['registry_file']))
                require(tunnel_config.get('environment_id') == target['id'] and
                        tunnel_config.get('resource_id') == controls[0]['resource_id'] and
                        tunnel_config.get('runtime_private_address') == controls[0]['private_ipv4'] and
                        tunnel_config.get('base_domain') == dns_config['base_domain'],
                        'CLUSTER_EDGE_TUNNEL_SCOPE_MISMATCH')
        else:
            require('tunnel' not in config, 'CLUSTER_EDGE_TUNNEL_SCOPE_MISMATCH')
    timeout = config.get('timeout_seconds', 1800)
    require(type(timeout) is int and 60 <= timeout <= 3600, 'CLUSTER_EDGE_TIMEOUT_INVALID')
    routes = [values] if target['provider'] == 'gcp' else list(values['routes'].values())
    fixed_routes = [{'hostname': route.get('hostname', route.get('host')), 'node_port': route['node_port'],
                     'health_path': route['health_path'], 'path_prefix': route.get('path_prefix', '/'), 'expected_status': 200}
                    for route in routes]
    return {'config': config, 'values': values, 'fixed_routes': fixed_routes, 'timeout_seconds': timeout, 'workers': copy.deepcopy(workers),
            'binding_sha256': digest({'config': config, 'values': values, 'authority_hashes': authority_hashes, 'nodes': [{key: node[key] for key in
                ('id', 'resource_id', 'private_ipv4', 'availability_zone')} for node in nodes]})}


def render_values(config, values, workers):
    values = copy.deepcopy(values)
    provider = config['provider']
    primary = workers[0]
    if provider == 'gcp':
        require(values.get('project_id', config['account_id']) == config['account_id'], 'CLUSTER_EDGE_SCOPE_MISMATCH')
        for key, expected in {'instance_name': primary['resource_id'].rsplit('/', 1)[1],
                'expected_private_ip': primary['private_ipv4'], 'zone': primary['availability_zone']}.items():
            require(values.get(key, expected) == expected, 'CLUSTER_EDGE_PRIMARY_COMPUTE_MISMATCH')
            values[key] = expected
        values['project_id'] = config['account_id']
        require(not values.get('routes'), 'CLUSTER_EDGE_PRIMARY_ROUTE_REQUIRED')
        require(values.get('certificate_dns_owner_id', config['cluster_id']) == config['cluster_id'], 'CLUSTER_EDGE_DNS_OWNER_MISMATCH')
        values['certificate_dns_owner_id'] = config['cluster_id']
        routes = [values]
    else:
        require(isinstance(values.get('routes'), dict) and 1 <= len(values['routes']) <= 50, 'CLUSTER_EDGE_ROUTES_REQUIRED')
        routes = list(values['routes'].values())
        if provider == 'aws':
            require(values.get('account_id', config['account_id']) == config['account_id'] and
                    values.get('region', config['region']) == config['region'], 'CLUSTER_EDGE_SCOPE_MISMATCH')
            values.update(account_id=config['account_id'], region=config['region'])
            for key in ('certificate_arn', 'apex_certificate_arn'):
                certificate = values.get(key)
                require(certificate is None or (isinstance(certificate, str) and re.fullmatch('arn:aws:acm:' + re.escape(config['region']) + ':' + re.escape(config['account_id']) + r':certificate/[a-f0-9-]+', certificate)), 'CLUSTER_EDGE_CERTIFICATE_SCOPE_MISMATCH')
        else:
            # A new stage has no old listener to disable or drain.
            require(values.get('enabled', True) is True, 'CLUSTER_EDGE_LISTENER_DISABLED')
            values['enabled'] = True
    for route in routes:
        require(isinstance(route, dict), 'CLUSTER_EDGE_ROUTE_INVALID')
        host = route.get('hostname') if provider == 'gcp' else route.get('host')
        require(isinstance(host, str) and len(host) <= 253 and re.fullmatch(FQDN, host), 'CLUSTER_EDGE_ROUTE_INVALID')
        require(type(route.get('node_port')) is int and 30000 <= route['node_port'] <= 32767 and
                path_valid(route.get('health_path')) and path_valid(route.get('path_prefix', '/')),
                'CLUSTER_EDGE_ROUTE_INVALID')
        expected = route.get('expected_status', 200)
        require(type(expected) is int and expected == 200, 'CLUSTER_EDGE_HEALTH_STATUS_INVALID')
        if provider != 'gcp':
            route['expected_status'] = expected
            require(route.get('target_private_ip', primary['private_ipv4']) == primary['private_ipv4'],
                    'CLUSTER_EDGE_PRIMARY_COMPUTE_MISMATCH')
            route['target_private_ip'] = primary['private_ipv4']
            if provider == 'aws':
                sg = config['worker_bindings'][primary['resource_id']]['security_group_id']
                require(route.get('target_security_group_id', sg) == sg and route.get('provider_kind', 'aws') == 'aws' and
                        route.get('manage_dns', True) is True, 'CLUSTER_EDGE_ROUTE_SCOPE_MISMATCH')
                route.update(provider_kind='aws', target_security_group_id=sg, manage_dns=True)
            else:
                subnet = config['worker_bindings'][primary['resource_id']]['subnet_id']
                require(route.get('member_subnet_id', subnet) == subnet, 'CLUSTER_EDGE_ROUTE_SCOPE_MISMATCH')
                route['member_subnet_id'] = subnet
    values['compute_nodes'] = {}
    for node in workers:
        value = {'private_ip': node['private_ipv4'], 'availability_zone': node['availability_zone']}
        if provider == 'gcp':
            value['instance_name'] = node['resource_id'].rsplit('/', 1)[1]
        else:
            value.update(resource_id=node['resource_id'].rsplit('/', 1)[1] if provider == 'aws' else node['resource_id'], **config['worker_bindings'][node['resource_id']])
        values['compute_nodes'][node['id']] = value
    return values


@contextmanager
def locked(directory):
    root = private_directory(directory)
    with os.fdopen(os.open(root / 'edge.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), 'a') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                'CLUSTER_EDGE_PRIVATE_STATE_REQUIRED')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EdgeError('CLUSTER_EDGE_BUSY') from None
        yield root


def render(prepared, directory):
    """Materialize reviewed modules and bound tfvars into an isolated private stage."""
    directory = private_directory(directory)
    source = ROOT / 'infrastructure/terraform' / (prepared['config']['provider'] + '-edge')
    files = {path.name: path.read_bytes() for path in source.iterdir()
             if path.suffix == '.tf' or path.name == '.terraform.lock.hcl'}
    require('.terraform.lock.hcl' in files and all(not re.search(rb'\bbackend\s+"', body) for body in files.values()),
            'CLUSTER_EDGE_LOCKED_MODULE_REQUIRED')
    module_digest = digest({name: hashlib.sha256(body).hexdigest() for name, body in files.items()})
    for name, body in files.items():
        durable_write(directory / name, body)
    durable_write(directory / 'terraform.tfvars.json', encoded(prepared['values']))
    return module_digest


def source_digest(provider):
    source = ROOT / 'infrastructure/terraform' / (provider + '-edge')
    return digest({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()
                   if path.suffix == '.tf' or path.name == '.terraform.lock.hcl'})


def state_identity(path, provider):
    state = private_json(path)
    require(state.get('version') == 4 and isinstance(state.get('lineage'), str) and state['lineage'] and
            type(state.get('serial')) is int, 'CLUSTER_EDGE_STATE_INVALID')
    owned = {}
    for resource in state.get('resources', []):
        if resource.get('mode') == 'data':
            continue
        require(resource.get('mode') == 'managed' and resource.get('type', '').startswith(PROVIDERS[provider]) and
                not resource.get('module'), 'CLUSTER_EDGE_STATE_FOREIGN')
        for instance in resource.get('instances', []):
            address = resource['type'] + '.' + resource['name']
            if 'index_key' in instance:
                address += '[' + json.dumps(instance['index_key'], separators=(',', ':')) + ']'
            identity = instance.get('attributes', {}).get('id')
            require(not instance.get('deposed') and address not in owned and isinstance(identity, str) and identity,
                    'CLUSTER_EDGE_STATE_INVALID')
            owned[address] = identity
    require(owned, 'CLUSTER_EDGE_STATE_EMPTY')
    return {'lineage': state['lineage'], 'serial': state['serial'], 'owned_resources': owned,
            'state_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()}


def validate_plan(plan, provider, owned):
    require(not plan.get('errored') and plan.get('complete', True), 'CLUSTER_EDGE_PLAN_INCOMPLETE')
    planned = {item['address']: item for item in plan.get('resource_changes', [])}
    for drift in plan.get('resource_drift', []):
        identity = owned.get(drift.get('address'))
        change = drift.get('change', {})
        item = planned.get(drift.get('address'), {})
        require(identity is not None and change.get('actions') in (['no-op'], ['update']) and
                item.get('change', {}).get('actions') == ['no-op'] and not drift.get('previous_address') and
                not drift.get('deposed') and not change.get('importing') and not change.get('replace_paths') and
                isinstance(change.get('before'), dict) and isinstance(change.get('after'), dict) and
                change['before'].get('id') == change['after'].get('id') == identity and
                not change.get('after_unknown', {}).get('id'), 'CLUSTER_EDGE_DRIFT_REQUIRES_REVIEW')
    seen = set()
    kinds = set()
    for item in plan.get('resource_changes', []):
        change, address = item['change'], item['address']
        if item.get('mode') == 'data' and change['actions'] in (['read'], ['no-op']):
            continue
        require(item.get('mode') == 'managed' and address not in seen and item.get('type', '').startswith(PROVIDERS[provider]) and
                not item.get('previous_address') and not item.get('deposed') and not change.get('importing') and
                not change.get('replace_paths'), 'CLUSTER_EDGE_PLAN_FOREIGN')
        kinds.add(item['type'])
        if address in owned:
            require(change['actions'] == ['no-op'] and isinstance(change.get('before'), dict) and
                    isinstance(change.get('after'), dict) and
                    change['before'].get('id') == change['after'].get('id') == owned[address] and
                    not change.get('after_unknown', {}).get('id'),
                    'CLUSTER_EDGE_EXISTING_CHANGE_REQUIRES_REVIEW')
        else:
            require(not owned and change['actions'] == ['create'] and change.get('before') is None,
                    'CLUSTER_EDGE_PLAN_DESTRUCTIVE')
        seen.add(address)
    require(bool(seen) and (not owned or seen == set(owned)), 'CLUSTER_EDGE_PLAN_OWNERSHIP_MISMATCH')
    require(kinds <= EDGE_KINDS[provider], 'CLUSTER_EDGE_NON_EDGE_RESOURCE')
    return seen


def tunnel_renderer():
    spec = importlib.util.spec_from_file_location('railshot_cluster_tunnel_render', ROOT / 'deployment/cloudflared/render.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def matches(expected, observed):
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(key in observed and matches(value, observed[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(observed, list) and len(expected) == len(observed) and all(matches(left, right) for left, right in zip(expected, observed))
    return type(expected) is type(observed) and expected == observed


def tunnel_object_matches(expected, observed):
    expected = copy.deepcopy(expected)
    if expected.get('kind') == 'Deployment':
        wanted = expected['spec']['template']['spec']
        pod = observed.get('spec', {}).get('template', {}).get('spec', {}) if isinstance(observed, dict) else {}
        for key in ('hostNetwork', 'hostPID', 'hostIPC'):
            if wanted.get(key) is False and key not in pod:
                wanted.pop(key)
    return matches(expected, observed)


def cluster_tunnel(config, request, kube, directory, *, observe=False):
    """Create a fresh cluster's connector, or observe exact recorded object UIDs.

    A pre-existing unbound object is a conflict. Secrets cross only the private
    stdin Kubernetes transport, never argv, checked-in manifests or returned logs.
    The parent edge journal records write intent before this function is called.
    """
    renderer = tunnel_renderer()
    documents = renderer.render(**{key: config[key] for key in
        ('namespace', 'name', 'tunnel_id', 'credentials_secret', 'origin_vip')},
        ca_configmap=config.get('ca_configmap'), hostnames=[request['hostname']])['items']
    namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': config['namespace']}}
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': config['credentials_secret'], 'namespace': config['namespace']},
              'type': 'Opaque', 'data': {'credentials.json': base64.b64encode(encoded(private_json(config['credentials_file']))).decode()}}
    documents = [namespace, secret, *documents]
    if config.get('ca_file'):
        # Public CA material still uses an owned private file and stdin transport.
        ca = Path(config['ca_file'])
        info = ca.lstat()
        require(ca.is_absolute() and ca.resolve() == ca and stat.S_ISREG(info.st_mode) and
                info.st_uid == os.geteuid() and not info.st_mode & 0o077 and 0 < info.st_size <= 1_000_000,
                'CLUSTER_EDGE_TUNNEL_CA_INVALID')
        content = ca.read_text()
        require('-----BEGIN CERTIFICATE-----' in content and 'PRIVATE KEY' not in content, 'CLUSTER_EDGE_TUNNEL_CA_INVALID')
        documents.insert(2, {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': config['ca_configmap'], 'namespace': config['namespace']},
                             'data': {'ca.pem': content}})
    owner = config['cluster_id']
    for document in documents:
        document['metadata'].setdefault('labels', {})['railshot.io/cluster-edge-owner'] = owner
        if document['kind'] == 'ConfigMap' and document['metadata']['name'] == config['name'] + '-config':
            document['metadata']['annotations'] = {'railshot.io/application-owners': json.dumps({request['hostname']: request['application_id']}, separators=(',', ':'))}
        if document['kind'] == 'Deployment':
            # The default control-plane taint and an explicit exclusion keep the connector on compute.
            document['spec']['template']['spec']['affinity'] = {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {
                'nodeSelectorTerms': [{'matchExpressions': [{'key': 'node-role.kubernetes.io/control-plane', 'operator': 'DoesNotExist'}]}]}}}
    identities_file = directory / 'tunnel-objects.json'
    identities = private_json(identities_file) if identities_file.exists() else {}
    require(not observe or identities, 'CLUSTER_EDGE_TUNNEL_READBACK_MISSING')
    deployment = None
    for document in documents:
        meta = document['metadata']
        ns = meta.get('namespace', '')
        key = document['kind'] + '/' + meta['name']
        observed = kube(ns, 'get', document['kind'], meta['name'], '--ignore-not-found', '-o', 'json')
        if not observed:
            require(not observe and key not in identities, 'CLUSTER_EDGE_TUNNEL_OBJECT_MISSING')
            observed = kube(ns, 'create', '-f', '-', '-o', 'json', document=document)
            require(isinstance(observed, dict) and tunnel_object_matches(document, observed), 'CLUSTER_EDGE_TUNNEL_CREATE_UNVERIFIED')
            uid = observed.get('metadata', {}).get('uid')
            require(isinstance(uid, str) and uid, 'CLUSTER_EDGE_TUNNEL_CREATE_UNVERIFIED')
            identities[key] = uid
            durable_write(identities_file, encoded(identities))
        else:
            require(key in identities and observed.get('metadata', {}).get('uid') == identities[key] and
                    tunnel_object_matches(document, observed), 'CLUSTER_EDGE_TUNNEL_OBJECT_CONFLICT')
        if document['kind'] in ('Secret', 'ConfigMap'):
            require(set(observed.get('data', {})) == set(document.get('data', {})) and not observed.get('binaryData'), 'CLUSTER_EDGE_TUNNEL_OBJECT_CONFLICT')
        if document['kind'] == 'ServiceAccount':
            require(not observed.get('imagePullSecrets') and not observed.get('secrets'), 'CLUSTER_EDGE_TUNNEL_OBJECT_CONFLICT')
        if document['kind'] == 'Deployment':
            deployment = observed
            pod = observed.get('spec', {}).get('template', {}).get('spec', {})
            require(not pod.get('initContainers') and not pod.get('ephemeralContainers') and
                    all(row.get('key') in ('node.kubernetes.io/not-ready', 'node.kubernetes.io/unreachable') and
                        row.get('operator') == 'Exists' and row.get('effect') == 'NoExecute' and row.get('tolerationSeconds') == 300
                        for row in pod.get('tolerations', [])) and
                    not any(container.get('env') or container.get('envFrom') or container.get('ports') for container in pod.get('containers', [])),
                    'CLUSTER_EDGE_TUNNEL_OBJECT_CONFLICT')
        require(not observed.get('metadata', {}).get('ownerReferences') and not observed.get('metadata', {}).get('deletionTimestamp'),
                'CLUSTER_EDGE_TUNNEL_OBJECT_CONFLICT')
    # Deployment availability is a separate backend check, not public HTTPS.
    require(set(identities) == {document['kind'] + '/' + document['metadata']['name'] for document in documents},
            'CLUSTER_EDGE_TUNNEL_OBJECT_CONFLICT')
    return {'status': 'succeeded', 'hostname': request['hostname'], 'https_verified': False,
            'tunnel_id': config['tunnel_id'], 'connector_available': deployment.get('status', {}).get('availableReplicas', 0) == 1,
            'dns': {'type': 'CNAME', 'content': config['tunnel_id'] + '.cfargotunnel.com', 'proxied': True}}


class NativeRunner:
    def __init__(self, prepared, deadline, env=None, kube=None):
        self.prepared, self.deadline, self.kube = prepared, deadline, kube
        self.env = credential_env(prepared['config'], os.environ if env is None else env)
        self.env.update(TF_IN_AUTOMATION='1', AWS_PAGER='')
        self.env['PYTHONPATH'] = str(ROOT / 'gitops') + os.pathsep + str(ROOT / 'ci/scripts')
        if prepared['config']['provider'] == 'gcp':
            self.env['RAILSHOT_GCP_CERTIFICATE_DNS_CONFIG'] = prepared['config']['dns_config_file']
        if prepared['config']['provider'] == 'openstack':
            self.env['OS_REGION_NAME'] = prepared['config']['region']

    def __call__(self, args, *, cwd, timeout=300):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, 'CLUSTER_EDGE_DEADLINE_EXCEEDED')
        try:
            process = subprocess.Popen(args, cwd=cwd, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                stdout, _ = process.communicate(timeout=min(timeout, remaining))
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
            require(process.returncode == 0, 'CLUSTER_EDGE_NATIVE_FAILED')
            return stdout
        except (OSError, subprocess.TimeoutExpired):
            raise EdgeError('CLUSTER_EDGE_NATIVE_FAILED') from None

    def worker(self, operation, config, request):
        # The worker and all of its children share the remaining request deadline.
        directory = self.directory
        request_file = directory / (operation + '-request.json')
        durable_write(request_file, encoded(request))
        return json.loads(self([sys.executable, str(Path(__file__).resolve()), operation,
                                config, str(request_file)], cwd=directory, timeout=300))

    def dns(self, config, request, *, observe=False):
        return self.worker('dns-readback' if observe else 'dns', config, request)

    def tunnel(self, config, request, *, observe=False):
        authority = private_json(config)
        mutable = self.directory / 'application-tunnel.json'
        if observe and authority.get('profile') == 'kubeadm-1c2w' and mutable.exists():
            return self.worker('tunnel-readback', str(mutable), request)
        if authority.get('profile') == 'kubeadm-1c2w':
            require(self.kube is not None, 'CLUSTER_EDGE_KUBE_TRANSPORT_REQUIRED')
            require(time.monotonic() < self.deadline, 'CLUSTER_EDGE_DEADLINE_EXCEEDED')
            return cluster_tunnel(authority, request, self.kube, self.directory, observe=observe)
        return self.worker('tunnel-readback' if observe else 'tunnel', config, request)


def dns_readback(runner, config_file, request, *, observe):
    observed = runner.dns(config_file, request, observe=observe)
    require(isinstance(observed, dict) and observed.get('status') == 'verified' and
            all(observed.get(key) == value for key, value in request.items()), 'CLUSTER_EDGE_DNS_UNVERIFIED')
    return observed


def configured_receipt(prepared, outputs, runner, *, observe=False):
    """Native/DNS API readback means configured; live health is left unverified."""
    provider, config = prepared['config']['provider'], prepared['config']
    values = prepared['values']
    output = {name: row['value'] for name, row in outputs.items()}
    if provider == 'aws':
        require(isinstance(output.get('alb_dns_name'), str) and output['alb_dns_name'].endswith('.elb.amazonaws.com') and
                set(output.get('target_group_arns', {})) == set(values['routes']), 'CLUSTER_EDGE_OUTPUT_MISMATCH')
        hostname = next(iter(values['routes'].values()))['host']
        certificate = output.get('certificate_arn')
        require(isinstance(certificate, str) and certificate.startswith('arn:aws:acm:' + config['region'] + ':' + config['account_id'] + ':certificate/'), 'CLUSTER_EDGE_CERTIFICATE_SCOPE_MISMATCH')
        proof = {'dns': 'route53-managed', 'certificate': certificate}
    elif provider == 'gcp':
        try:
            frontend = ipaddress.IPv4Address(output['frontend_ip'])
        except (KeyError, ValueError, TypeError):
            raise EdgeError('CLUSTER_EDGE_OUTPUT_MISMATCH') from None
        require(frontend.is_global and isinstance(output.get('backend_service'), str), 'CLUSTER_EDGE_OUTPUT_MISMATCH')
        auth = output.get('dns_authorization_record')
        require(isinstance(auth, dict) and auth.get('type') == 'CNAME' and
                auth.get('data', '').rstrip('.').endswith('.authorize.certificatemanager.goog'), 'CLUSTER_EDGE_CERTIFICATE_DNS_INVALID')
        certificate_request = {'application_id': config['cluster_id'], 'purpose': 'certificate',
            'application_hostname': values['hostname'], 'hostname': auth['name'].rstrip('.'), 'type': 'CNAME',
            'content': auth['data'].rstrip('.')}
        dns_readback(runner, config['dns_config_file'], certificate_request, observe=observe)
        dns_readback(runner, config['dns_config_file'], {'application_id': config['cluster_id'], 'hostname': values['hostname'],
            'type': 'A', 'content': str(frontend)}, observe=observe)
        hostname = values['hostname']
        proof = {'dns': 'configured-writer', 'certificate': 'certificate-manager-managed'}
    else:
        require(output.get('vip_address') == values['vip_address'] and
                set(output.get('pool_ids', {})) == set(values['routes']), 'CLUSTER_EDGE_OUTPUT_MISMATCH')
        tunnel = config['tunnel']
        observed = runner.tunnel(tunnel['config_file'], tunnel['request'], observe=observe)
        tc = private_json(tunnel['config_file'])
        require(observed.get('tunnel_id') == tc['tunnel_id'] and observed.get('dns') ==
                {'type': 'CNAME', 'content': tc['tunnel_id'] + '.cfargotunnel.com', 'proxied': True},
                'CLUSTER_EDGE_TUNNEL_UNVERIFIED')
        hostname = tunnel['request']['hostname']
        dns_readback(runner, config['dns_config_file'], {'application_id': tunnel['request']['application_id'],
            'hostname': hostname, **observed['dns']}, observe=observe)
        proof = {'dns': 'configured-writer', 'certificate': values['default_tls_container_ref']}
    return {'edge_ready': True, 'configured': True, 'provider': provider, 'hostname': hostname,
            'compute_ids': [node['resource_id'] for node in prepared['workers']], 'binding_sha256': prepared['binding_sha256'], 'proof': proof,
            'lb_healthy': False, 'dns_resolved': False, 'tls_verified': False, 'public_http_verified': False}


def openstack_compute_admission(prepared, runner, directory):
    """Observe exact Nova project/AZ and Neutron IP/subnet ownership before writes."""
    config = prepared['config']
    for worker in prepared['workers']:
        server = json.loads(runner(['openstack', 'server', 'show', worker['resource_id'], '-f', 'json'], cwd=directory))
        require(server.get('id') == worker['resource_id'] and
                server.get('project_id', server.get('tenant_id')) == config['account_id'] and
                server.get('OS-EXT-AZ:availability_zone', server.get('availability_zone')) == worker['availability_zone'],
                'CLUSTER_EDGE_COMPUTE_ADMISSION_FAILED')
        ports = json.loads(runner(['openstack', 'port', 'list', '--server', worker['resource_id'], '-f', 'json', '-c', 'ID'], cwd=directory))
        require(isinstance(ports, list) and 1 <= len(ports) <= 16, 'CLUSTER_EDGE_COMPUTE_ADMISSION_FAILED')
        matches_found = 0
        for port in ports:
            identity = port.get('ID')
            require(isinstance(identity, str) and re.fullmatch(UUID, identity), 'CLUSTER_EDGE_COMPUTE_ADMISSION_FAILED')
            observed = json.loads(runner(['openstack', 'port', 'show', identity, '-f', 'json'], cwd=directory))
            require(observed.get('device_id') == worker['resource_id'] and
                    observed.get('project_id', observed.get('tenant_id')) == config['account_id'], 'CLUSTER_EDGE_COMPUTE_ADMISSION_FAILED')
            matches_found += sum(row.get('ip_address') == worker['private_ipv4'] and row.get('subnet_id') ==
                config['worker_bindings'][worker['resource_id']]['subnet_id'] for row in observed.get('fixed_ips', []))
        require(matches_found == 1, 'CLUSTER_EDGE_COMPUTE_ADMISSION_FAILED')


def provision(request, *, state_dir, runner=None, deadline=None, env=None, kube=None, expected_binding_sha256=None):
    """Execute once in a private cluster stage; repeat only a recorded successful scope.

    runner is an injected command/DNS/Tunnel transport for offline contract tests.
    No credentials or provider command output is returned to callers.
    """
    require(deadline is None or time.monotonic() < deadline, 'CLUSTER_EDGE_DEADLINE_EXCEEDED')
    prepared = validate(request)
    require(expected_binding_sha256 is None or expected_binding_sha256 == prepared['binding_sha256'], 'CLUSTER_EDGE_SCOPE_CHANGED')
    require(deadline is None or time.monotonic() < deadline, 'CLUSTER_EDGE_DEADLINE_EXCEEDED')
    config = prepared['config']
    directory = Path(state_dir) / config['cluster_id']
    with locked(directory) as root:
        journal_file, state_file = root / 'journal.json', root / 'terraform.tfstate'
        journal = private_json(journal_file) if journal_file.exists() else None
        replay = bool(journal and journal.get('phase') == 'configured')
        applications_record = None
        if journal:
            require(journal.get('version') == 1 and journal.get('phase') in ('prepared', 'applying', 'configuring', 'configured'), 'CLUSTER_EDGE_JOURNAL_INVALID')
            require(journal['binding_sha256'] == prepared['binding_sha256'], 'CLUSTER_EDGE_SCOPE_CHANGED')
            if journal['phase'] in ('applying', 'configuring'):
                raise EdgeError('CLUSTER_EDGE_OUTCOME_UNKNOWN', unknown=True)
            if replay:
                if (root / 'applications.json').exists():
                    import cluster_applications
                    cluster_applications.recover(request, prepared, root)
                    applications_record = cluster_applications.state(root, prepared)
                    prepared['values'] = applications_record['values']
                else:
                    require(state_identity(state_file, config['provider']) == journal['state'], 'CLUSTER_EDGE_STATE_CHANGED')
                require(journal['module_sha256'] == source_digest(config['provider']), 'CLUSTER_EDGE_SOURCE_CHANGED')
        else:
            # Reserve an empty stage before Terraform starts. Unowned state is never adopted.
            require(not state_file.exists() and not any(root.glob('*.tfstate*')) and
                    not (root / '.terraform').exists(), 'CLUSTER_EDGE_STATE_ADOPTION_FORBIDDEN')
            journal = {'version': 1, 'binding_sha256': prepared['binding_sha256'], 'phase': 'prepared'}
            durable_write(journal_file, encoded(journal))
        require(replay or not state_file.exists(), 'CLUSTER_EDGE_PARTIAL_STATE_REQUIRES_REVIEW')
        if not replay:
            journal['module_sha256'] = render(prepared, root)
            durable_write(journal_file, encoded(journal))
        edge_deadline = time.monotonic() + prepared['timeout_seconds']
        executor = runner or NativeRunner(prepared, min(edge_deadline, deadline) if deadline is not None else edge_deadline, env, kube)
        if isinstance(executor, NativeRunner):
            executor.directory = root
        def tf(*args):
            require(deadline is None or time.monotonic() < deadline, 'CLUSTER_EDGE_DEADLINE_EXCEEDED')
            return executor(['terraform', *args], cwd=root, timeout=prepared['timeout_seconds'])
        # Native OpenStack provider must be scoped to the supplied project as well as region.
        if config['provider'] == 'aws' and isinstance(executor, NativeRunner):
            caller = json.loads(executor(['aws', 'sts', 'get-caller-identity', '--output', 'json', '--no-cli-pager'], cwd=root))
            require(caller.get('Account') == config['account_id'], 'CLUSTER_EDGE_SCOPE_MISMATCH')
        if config['provider'] == 'openstack':
            token = json.loads(executor(['openstack', 'token', 'issue', '-f', 'json'], cwd=root))
            require(token.get('project_id') == config['account_id'], 'CLUSTER_EDGE_SCOPE_MISMATCH')
            if isinstance(executor, NativeRunner):
                openstack_compute_admission(prepared, executor, root)
        tf('init', '-input=false', '-no-color', '-lockfile=readonly')
        tf('validate', '-no-color')
        plan_name = 'readback.plan' if replay else 'edge.plan'
        tf('plan', '-input=false', '-no-color', '-lock-timeout=0s', '-out=' + plan_name)
        saved_plan_sha256 = hashlib.sha256((root / plan_name).read_bytes()).hexdigest()
        plan = json.loads(tf('show', '-json', plan_name))
        require(hashlib.sha256((root / plan_name).read_bytes()).hexdigest() == saved_plan_sha256, 'CLUSTER_EDGE_PLAN_CHANGED')
        if replay:
            validate_plan(plan, config['provider'], (applications_record or journal)['state']['owned_resources'])
            receipt = configured_receipt(prepared, json.loads(tf('output', '-json')), executor, observe=True)
            if applications_record:
                for application in applications_record['apps'].values():
                    require(application['status'] != 'partial', 'CLUSTER_APPLICATION_RECONCILIATION_REQUIRED')
                    if application['status'] == 'started':
                        cluster_applications.public_route(prepared, root, application['request'], executor, observe=True)
            # No cloud writes; a fresh observation replaces only the private local receipt.
            journal['receipt'] = receipt
            durable_write(journal_file, encoded(journal))
            return receipt
        owned = validate_plan(plan, config['provider'], {})
        journal.update(phase='applying', plan_sha256=saved_plan_sha256)
        durable_write(journal_file, encoded(journal))
        try:
            require(hashlib.sha256((root / 'edge.plan').read_bytes()).hexdigest() == saved_plan_sha256, 'CLUSTER_EDGE_PLAN_CHANGED')
            tf('apply', '-input=false', '-no-color', '-lock-timeout=0s', 'edge.plan')
            identity = state_identity(state_file, config['provider'])
            require(set(identity['owned_resources']) == owned, 'CLUSTER_EDGE_APPLIED_OWNERSHIP_MISMATCH')
            journal.update(phase='configuring', state=identity)
            durable_write(journal_file, encoded(journal))
            tf('plan', '-input=false', '-no-color', '-lock-timeout=0s', '-out=readback.plan')
            validate_plan(json.loads(tf('show', '-json', 'readback.plan')), config['provider'], identity['owned_resources'])
            # Refresh is an observation until applied; this check permits only exact no-op identities.
            receipt = configured_receipt(prepared, json.loads(tf('output', '-json')), executor)
            journal.update(phase='configured', receipt=receipt)
            durable_write(journal_file, encoded(journal))
            return receipt
        except Exception:
            # Terraform, DNS and tunnel commands can write before reporting failure.
            raise EdgeError('CLUSTER_EDGE_OUTCOME_UNKNOWN', unknown=True) from None


def worker_main():
    """Private child boundary, used only to bound DNS/tunnel operations as a group."""
    os.umask(0o077)
    require(len(sys.argv) == 4 and sys.argv[1] in ('dns', 'tunnel', 'dns-readback', 'tunnel-readback'), 'CLUSTER_EDGE_WORKER_INPUT_INVALID')
    operation, config_file, request_file = sys.argv[1:]
    request = private_json(request_file)
    if operation == 'dns':
        result = dns.ensure(config_file, request)
    elif operation == 'dns-readback':
        config = dns.config_at(config_file)
        request = dns.request_at(config, request)
        record = dns.owned_record(dns.records_at(config, request['hostname']), request)
        require(record is not None, 'CLUSTER_EDGE_DNS_READBACK_MISSING')
        result = {'status': 'verified', **request, 'record_id': record['id']}
    else:
        spec = importlib.util.spec_from_file_location('railshot_cluster_tunnel', ROOT / 'deployment/cloudflared/register.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if operation == 'tunnel-readback':
            with module.lifecycle_session(config_file, request) as (config, kube):
                observed = module.lifecycle_snapshot(config, kube, request)
                require(observed['owners'].get(request['hostname']) == request['application_id'], 'CLUSTER_EDGE_TUNNEL_UNVERIFIED')
            result = {'status': 'succeeded', 'hostname': request['hostname'], 'https_verified': False, 'tunnel_id': config['tunnel_id'],
                      'dns': {'type': 'CNAME', 'content': config['tunnel_id'] + '.cfargotunnel.com', 'proxied': True}}
        else:
            result = module.ensure(config_file, request)
        require(isinstance(result, dict) and result.get('status') == 'succeeded' and
                result.get('hostname') == request['hostname'] and result.get('https_verified') is False,
                'CLUSTER_EDGE_TUNNEL_UNVERIFIED')
    print(encoded(result).decode())


if __name__ == '__main__':
    try:
        worker_main()
    except Exception:
        # Provider output and credential-bearing traceback must stay private.
        print('{"error":"CLUSTER_EDGE_WORKER_FAILED"}')
        raise SystemExit(3) from None
