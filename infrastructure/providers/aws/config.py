"""Offline operator configuration and explicit budget admission; no cloud calls."""
import datetime as dt
import base64
import importlib.util
import ipaddress
import math
import sys
from pathlib import Path
import re
import tempfile
from .native import AwsError, private_json, Session, save
ROOT = Path(__file__).resolve().parents[3]


def require_config(config):
    required = {'account_id', 'auth', 'ami_id', 'ssh_identity_file', 'ssh_public_key', 'edge_variables_file', 'application_template_file', 'budget_file', 'management_source_cidrs', 'observer_source_cidrs'}
    allowed = required | {'zones', 'vpc_cidr', 'instance_type', 'data_disk_gib', 'database_config_file'}
    if not isinstance(config, dict) or not required <= config.keys() or set(config) - allowed:
        raise AwsError('AWS_OPERATOR_PROFILE_INCOMPLETE')
    if not re.fullmatch(r'\d{12}', config['account_id']): raise AwsError('AWS_ACCOUNT_INVALID')
    Session(config['auth'], config['account_id']).base()
    if not re.fullmatch(r'ami-[0-9a-f]{17}', config['ami_id']): raise AwsError('AWS_AMI_INVALID')
    if not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/]+={0,3}( [^\r\n]+)?', config['ssh_public_key']): raise AwsError('AWS_SSH_PUBLIC_KEY_INVALID')
    cidr = ipaddress.ip_network(config.get('vpc_cidr', '10.72.0.0/16'), strict=True)
    if cidr.version != 4 or cidr.prefixlen != 16 or not any(cidr.subnet_of(ipaddress.ip_network(n)) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise AwsError('AWS_NETWORK_CIDR_INVALID')
    if any(cidr.overlaps(ipaddress.ip_network(n)) for n in ('10.244.0.0/16', '10.96.0.0/12')): raise AwsError('AWS_NETWORK_KUBERNETES_CIDR_OVERLAP')
    if config.get('instance_type', 't3.large') not in ('t3.large', 't3.xlarge', 't3.2xlarge'): raise AwsError('AWS_INSTANCE_TYPE_UNSUPPORTED')
    gib = config.get('data_disk_gib', 100)
    if type(gib) is not int or not 20 <= gib <= 1000: raise AwsError('AWS_DATA_DISK_INVALID')
    for field in ('management_source_cidrs', 'observer_source_cidrs'):
        if not isinstance(config[field], list) or not 1 <= len(config[field]) <= 16: raise AwsError('AWS_MANAGEMENT_SOURCE_REQUIRED')
        for value in config[field]:
            network = ipaddress.ip_network(value, strict=True)
            if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global: raise AwsError('AWS_MANAGEMENT_SOURCE_INVALID')
    sys.path[:0] = [str(ROOT / path) for path in ('deployment/scripts', 'infrastructure/ansible', 'gitops', 'ci/scripts')]
    from database import private_bytes
    private_bytes(config['ssh_identity_file'])
    if config.get('database_config_file'):
        from standalone import validate_config as validate_database
        if not validate_database(private_json(config['database_config_file']))['valid']: raise AwsError('AWS_DATABASE_CONFIG_INVALID')
    for field in ('edge_variables_file', 'application_template_file', 'budget_file'):
        private_json(config[field])
    edge = private_json(config['edge_variables_file'])
    domain = r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}'
    if not re.fullmatch(r'Z[A-Z0-9]{5,31}', edge.get('zone_id', '')) or not re.fullmatch(domain, edge.get('base_domain', '')) or not isinstance(edge.get('routes'), dict) or not edge['routes']:
        raise AwsError('AWS_DNS_PROFILE_REQUIRED')
    if edge.get('certificate_arn') is not None and not re.fullmatch(r'arn:aws:acm:ap-northeast-2:' + config['account_id'] + r':certificate/[a-f0-9-]{36}', edge['certificate_arn']): raise AwsError('AWS_CERTIFICATE_SCOPE_INVALID')
    for route in edge['routes'].values():
        host = route.get('host', '')
        if not re.fullmatch(domain, host) or not (host == edge['base_domain'] or host.endswith('.' + edge['base_domain'])) or type(route.get('node_port')) is not int or not 30000 <= route['node_port'] <= 32767 or not re.fullmatch(r'/[A-Za-z0-9/._~-]*', route.get('health_path', '')) or type(route.get('priority')) is not int or not 1 <= route['priority'] <= 50000:
            raise AwsError('AWS_EDGE_ROUTE_INVALID')
    template = private_json(config['application_template_file'])
    if set(template) != {'version', 'cd', 'profile'} or type(template['version']) is not int or template['version'] != 1 or template['profile'].get('provider') != 'aws': raise AwsError('AWS_APPLICATION_TEMPLATE_INVALID')
    import applications
    with tempfile.TemporaryDirectory(prefix='railshot-aws-validation-') as work:
        root = Path(work); path = root / 'application.json'
        save(path, {'version': 1, 'state_dir': str(root / 'state'), 'registry_file': str(root / 'registry.json'),
            'cd': template['cd'], 'environments': {'env-validation': template['profile']}})
        applications.load_config(path)
    profile = template['profile']; ingress = profile['ingress']
    if ingress['base_domain'] != edge['base_domain']: raise AwsError('AWS_DOMAIN_BINDING_MISMATCH')
    pull = private_json(profile['pull_secret_file'])
    decoded = base64.b64decode(pull['auths']['ghcr.io']['auth'], validate=True).decode()
    if ':' not in decoded or not all(decoded.split(':', 1)): raise AwsError('AWS_PULL_CREDENTIAL_INVALID')
    token = private_json(ingress['ci_credentials_file'])
    if set(token) != {'token'} or not isinstance(token['token'], str) or not token['token'].strip(): raise AwsError('AWS_CI_CREDENTIAL_REQUIRED')
    observer = private_json(ingress['observability_config_file'])
    observer_spec = importlib.util.spec_from_file_location('aws_observer_validation', ROOT / 'observability/register.py')
    module = importlib.util.module_from_spec(observer_spec); observer_spec.loader.exec_module(module); module.settings(observer)
    if observer.get('observer_source_cidr') not in config['observer_source_cidrs']: raise AwsError('AWS_OBSERVER_SOURCE_MISMATCH')
    approved = private_json(config['budget_file'])
    database_mode = 'standalone' if approved.get('node_count') == 4 else 'none'
    if database_mode == 'standalone' and not config.get('database_config_file'): raise AwsError('AWS_DATABASE_CONFIG_REQUIRED')
    budget(config, {'database': {'mode': database_mode}})
    return {'valid': True, 'provider': 'aws', 'account_id': config['account_id'], 'region': 'ap-northeast-2', 'live_verified': False}


def budget(config, spec):
    value = private_json(config['budget_file'])
    fields = {'version', 'approval_id', 'account_id', 'region', 'expires_at', 'max_cost_usd', 'estimated_cost_usd', 'duration_hours', 'node_count', 'elastic_ip_count', 'instance_type', 'data_disk_gib', 'pricing_basis', 'data_policy', 'ledger'}
    try:
        good = set(value) == fields and type(value['version']) is int and value['version'] == 1 and value['account_id'] == config['account_id'] and value['region'] == 'ap-northeast-2'
        good = good and type(value['node_count']) is int and value['node_count'] == (4 if spec['database']['mode'] == 'standalone' else 3)
        good = good and type(value['elastic_ip_count']) is int and value['elastic_ip_count'] == 3
        good = good and value['instance_type'] == config.get('instance_type', 't3.large') and value['data_disk_gib'] == config.get('data_disk_gib', 100)
        good = good and value['data_policy'] == 'retain' and isinstance(value['approval_id'], str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{2,127}', value['approval_id'])) and isinstance(value['pricing_basis'], str) and 10 <= len(value['pricing_basis']) <= 2048
        good = good and all(type(value[k]) in (int, float) and math.isfinite(value[k]) and value[k] > 0 for k in ('max_cost_usd', 'estimated_cost_usd', 'duration_hours'))
        good = good and value['estimated_cost_usd'] <= value['max_cost_usd']
        good = good and isinstance(value['ledger'], dict) and set(value['ledger']) == {'ledger_path', 'scope', 'currency', 'incremental_cost', 'limit', 'unreported_cost', 'quoted_at', 'expires_at'}
        good = good and value['ledger']['currency'] == 'USD' and float(value['ledger']['incremental_cost']) == value['estimated_cost_usd'] and float(value['ledger']['limit']) == value['max_cost_usd']
        good = good and dt.datetime.fromisoformat(value['expires_at'].replace('Z', '+00:00')) > dt.datetime.now(dt.timezone.utc)
    except (KeyError, TypeError, ValueError): good = False
    if not good: raise AwsError('AWS_BUDGET_APPROVAL_REQUIRED')
    return {k: value[k] for k in ('approval_id', 'max_cost_usd', 'estimated_cost_usd', 'duration_hours', 'pricing_basis', 'data_policy')}


def reserve_budget(config, spec, context):
    """Use the existing authoritative billing ledger before any native apply."""
    budget(config, spec)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'terraform_tools'))
    from provision import reserve_budget as reserve
    value = private_json(config['budget_file'])
    target = {'provider_kind': 'aws', 'variables': {'account_id': config['account_id']}, 'budget': value['ledger']}
    try:
        reserve(target, {'operation_id': 'environment:' + context['environment_id']})
    except Exception as error:
        raise AwsError('AWS_BUDGET_LEDGER_BLOCKED') from error


def validate_config(config):
    required = ('account_id', 'auth', 'ami_id', 'ssh_identity_file', 'ssh_public_key', 'edge_variables_file', 'application_template_file', 'budget_file', 'management_source_cidrs', 'observer_source_cidrs')
    missing = [key for key in required if not isinstance(config, dict) or not config.get(key)]
    if isinstance(config, dict) and isinstance(config.get('auth'), dict):
        missing += ['auth.' + k for k in ('mode', 'role_arn') if not config['auth'].get(k)]
        if config['auth'].get('mode') == 'customer_role' and not config['auth'].get('external_id'): missing.append('auth.external_id')
    if missing: return {'valid': False, 'missing': missing, 'errors': []}
    try:
        require_config(config)
        return {'valid': True, 'missing': [], 'errors': [], 'live_verified': False}
    except (AwsError, ValueError, TypeError, KeyError, OSError) as error:
        return {'valid': False, 'missing': [], 'errors': [getattr(error, 'code', 'AWS_OPERATOR_FILE_INVALID')]}


def config_template(directory):
    root = Path(directory)
    adapter = {'account_id': '', 'auth': {'mode': 'customer_role', 'role_arn': '', 'external_id': '', 'source_credentials_file': str(root / 'aws-credentials.json')},
        'ami_id': '', 'zones': ['ap-northeast-2a', 'ap-northeast-2c'], 'vpc_cidr': '10.72.0.0/16', 'instance_type': 't3.large',
        'data_disk_gib': 100, 'ssh_identity_file': '', 'ssh_public_key': '', 'management_source_cidrs': [], 'observer_source_cidrs': [], 'edge_variables_file': str(root / 'aws-edge.json'),
        'application_template_file': str(root / 'application-template.json'), 'budget_file': str(root / 'aws-budget.json'), 'database_config_file': ''}
    files = {
        'aws-credentials.json': {'AWS_ACCESS_KEY_ID': '', 'AWS_SECRET_ACCESS_KEY': '', 'AWS_SESSION_TOKEN': ''},
        'aws-edge.json': {'zone_id': '', 'base_domain': '', 'certificate_arn': None, 'routes': {'bootstrap': {'host': '', 'node_port': 30080, 'health_path': '/healthz', 'priority': 1}}},
        'application-template.json': {'version': 1, 'cd': {'version': 1, 'state_dir': '', 'repository': '', 'branch': '', 'context': ''},
            'profile': {'provider': 'aws', 'tenant': '', 'source_repository': '', 'pull_secret_file': '',
                'target': {'repo_url': '', 'architecture': 'amd64', 'ingress_cidrs': [], 'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '512Mi'}}},
                'ingress': {'base_domain': '', 'dns_config_file': '', 'observability_config_file': '', 'ci_credentials_file': ''}}},
        'aws-budget.json': {'version': 1, 'approval_id': '', 'account_id': '', 'region': 'ap-northeast-2', 'expires_at': '',
            'max_cost_usd': None, 'estimated_cost_usd': None, 'duration_hours': None, 'node_count': None, 'elastic_ip_count': 3,
            'instance_type': 't3.large', 'data_disk_gib': 100, 'pricing_basis': '', 'data_policy': 'retain',
            'ledger': {'ledger_path': '', 'scope': '', 'currency': 'USD', 'incremental_cost': '', 'limit': '', 'unreported_cost': '', 'quoted_at': '', 'expires_at': ''}}
    }
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ansible'))
    from standalone import config_template as postgres_template
    files.update(postgres_template(root)['files'])
    return {'adapter': adapter, 'files': files, 'notes': ['Fill blanks in private files; no generated approval is provided.', 'Do not paste credentials in chat or commit private files.', 'Budget approval, fresh authoritative billing ledger, delegated DNS and workload role must be supplied before live apply.']}
