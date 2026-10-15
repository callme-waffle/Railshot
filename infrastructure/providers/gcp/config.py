"""Offline operator inputs and existing authoritative budget admission."""
import datetime as dt
import base64
import importlib.util
import ipaddress
import math
from pathlib import Path
import re
import sys
import tempfile
from .native import GcpError, ROOT, private_json, save
from .auth import credential_identity, PROJECT

REQUIRED = {'project_id', 'project_number', 'auth', 'boot_image', 'ssh_identity_file', 'ssh_public_key', 'edge_variables_file', 'dns_config_file', 'application_template_file', 'budget_file', 'management_source_cidrs', 'observer_source_cidrs'}
OPTIONAL = {'zones', 'subnet_cidr', 'filestore_cidr', 'filestore_capacity_gib', 'machine_type', 'data_disk_gib', 'database_config_file'}


def require_config(config):
    if not isinstance(config, dict) or not REQUIRED <= set(config) or set(config) - REQUIRED - OPTIONAL: raise GcpError('GCP_OPERATOR_PROFILE_INCOMPLETE')
    if not re.fullmatch(PROJECT, config['project_id']) or not re.fullmatch(r'[0-9]{6,30}', config['project_number']): raise GcpError('GCP_PROJECT_INVALID')
    auth = config['auth']
    if set(auth) != {'mode', 'credentials_file', 'service_account'} or auth['mode'] not in ('workload_identity', 'managed_service_account'): raise GcpError('GCP_AUTH_CONFIGURATION_INVALID')
    authority = credential_identity(auth['credentials_file'], config['project_id'], auth['service_account'])
    if auth['mode'] == 'workload_identity' and authority['type'] != 'external_account': raise GcpError('GCP_WIF_REQUIRED')
    if not re.fullmatch(r'projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v[0-9]{8}', config['boot_image']): raise GcpError('GCP_PINNED_IMAGE_REQUIRED')
    if not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/]+={0,3}( [^\r\n]+)?', config['ssh_public_key']): raise GcpError('GCP_SSH_PUBLIC_KEY_INVALID')
    sys.path.insert(0, str(ROOT / 'infrastructure/ansible'))
    from database import private_bytes
    private_bytes(config['ssh_identity_file'])
    subnet = ipaddress.ip_network(config.get('subnet_cidr', '10.76.0.0/24'), strict=True)
    storage = ipaddress.ip_network(config.get('filestore_cidr', '10.77.0.0/26'), strict=True)
    private = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if any(n.version != 4 or not any(n.subnet_of(p) for p in private) for n in (subnet, storage)) or subnet.prefixlen != 24 or storage.prefixlen != 26 or subnet.overlaps(storage): raise GcpError('GCP_NETWORK_CIDR_INVALID')
    if any(n.overlaps(ipaddress.ip_network(p)) for n in (subnet, storage) for p in ('10.244.0.0/16', '10.96.0.0/12')): raise GcpError('GCP_NETWORK_KUBERNETES_CIDR_OVERLAP')
    zones = config.get('zones', ['asia-northeast3-a', 'asia-northeast3-b', 'asia-northeast3-c'])
    if not isinstance(zones, list) or len(zones) != 3 or len(set(zones)) != 3 or any(not re.fullmatch(r'asia-northeast3-[abc]', z) for z in zones): raise GcpError('GCP_ZONE_PROFILE_INVALID')
    for field, low, high, default in (('filestore_capacity_gib', 1024, 10240, 1024), ('data_disk_gib', 20, 1000, 100)):
        if type(config.get(field, default)) is not int or not low <= config.get(field, default) <= high: raise GcpError('GCP_CAPACITY_INVALID')
    if config.get('machine_type', 'e2-standard-4') not in ('e2-standard-2', 'e2-standard-4', 'e2-standard-8'): raise GcpError('GCP_MACHINE_TYPE_UNSUPPORTED')
    for field in ('management_source_cidrs', 'observer_source_cidrs'):
        if not isinstance(config[field], list) or not 1 <= len(config[field]) <= 16: raise GcpError('GCP_MANAGEMENT_SOURCE_REQUIRED')
        for cidr in config[field]:
            network = ipaddress.ip_network(cidr, strict=True)
            if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global: raise GcpError('GCP_MANAGEMENT_SOURCE_INVALID')
    edge = private_json(config['edge_variables_file'])
    if not re.fullmatch(r'(?:[a-z0-9-]+\.)+[a-z]{2,63}', edge.get('hostname', '')) or not isinstance(edge.get('health_path'), str) or not edge['health_path'].startswith('/'): raise GcpError('GCP_EDGE_PROFILE_INCOMPLETE')
    sys.path.insert(0, str(ROOT / 'gitops'))
    import dns
    dns.config_at(config['dns_config_file'])
    dns_config = private_json(config['dns_config_file']); private_bytes(dns_config['token_file'])
    template = private_json(config['application_template_file'])
    if set(template) != {'version', 'cd', 'profile'} or type(template['version']) is not int or template['version'] != 1: raise GcpError('GCP_APPLICATION_TEMPLATE_INVALID')
    profile = template['profile']; ingress = profile['ingress']
    if profile.get('provider') != 'gcp' or not profile.get('tenant') or not profile.get('source_repository') or not ingress.get('base_domain'): raise GcpError('GCP_APPLICATION_TEMPLATE_INCOMPLETE')
    private_json(profile['pull_secret_file'])
    for key in ('observability_config_file', 'ci_credentials_file'): private_json(ingress[key])
    observer = private_json(ingress['observability_config_file'])
    if observer.get('observer_source_cidr') not in config['observer_source_cidrs']: raise GcpError('GCP_OBSERVER_SOURCE_MISMATCH')
    ports = [observer.get('node_metrics_port'), observer.get('cluster_metrics_port')]
    if any(type(p) is not int or not 30000 <= p <= 32767 for p in ports) or len(set(ports)) != 2: raise GcpError('GCP_OBSERVER_PORT_INVALID')
    if not private_json(ingress['ci_credentials_file']).get('token'): raise GcpError('GCP_CI_CREDENTIAL_REQUIRED')
    for key in ('state_dir', 'repository'):
        if not isinstance(template['cd'][key], str) or not Path(template['cd'][key]).is_absolute(): raise GcpError('GCP_CD_PROFILE_INCOMPLETE')
    if not template['cd'].get('branch') or not template['cd'].get('context'): raise GcpError('GCP_CD_PROFILE_INCOMPLETE')
    sys.path[:0] = [str(ROOT / path) for path in ('deployment/scripts', 'infrastructure/ansible', 'gitops', 'ci/scripts')]
    import applications
    # Invoke the real application schema without creating a registry or calling a provider.
    with tempfile.TemporaryDirectory(prefix='railshot-gcp-validation-') as work:
        root = Path(work); path = root / 'application.json'
        save(path, {'version': 1, 'state_dir': str(root / 'state'), 'registry_file': str(root / 'registry.json'),
            'cd': template['cd'], 'environments': {'env-validation': profile}})
        applications.load_config(path)
    pull = private_json(profile['pull_secret_file'])
    decoded = base64.b64decode(pull['auths']['ghcr.io']['auth'], validate=True).decode()
    if ':' not in decoded or not all(decoded.split(':', 1)): raise GcpError('GCP_PULL_CREDENTIAL_INVALID')
    observer_spec = importlib.util.spec_from_file_location('gcp_observer_validation', ROOT / 'observability/register.py')
    observer_module = importlib.util.module_from_spec(observer_spec); observer_spec.loader.exec_module(observer_module)
    observer_module.settings(observer)
    private_json(config['budget_file'])
    if config.get('database_config_file'):
        from standalone import validate_config as validate_database
        if not validate_database(private_json(config['database_config_file']))['valid']: raise GcpError('GCP_DATABASE_CONFIG_INVALID')
    approved = private_json(config['budget_file'])
    database_mode = 'standalone' if approved.get('node_count') == 4 else 'none'
    if database_mode == 'standalone' and not config.get('database_config_file'): raise GcpError('GCP_DATABASE_CONFIG_REQUIRED')
    budget(config, {'database': {'mode': database_mode}})


def budget(config, spec, context=None):
    value = private_json(config['budget_file'])
    try:
        good = type(value['version']) is int and value['version'] == 1 and value['project_id'] == config['project_id'] and value['region'] == 'asia-northeast3'
        good = good and type(value['node_count']) is int and value['node_count'] == (4 if spec['database']['mode'] == 'standalone' else 3)
        good = good and value['machine_type'] == config.get('machine_type', 'e2-standard-4') and value['data_disk_gib'] == config.get('data_disk_gib', 100) and value['filestore_capacity_gib'] == config.get('filestore_capacity_gib', 1024)
        good = good and all(type(value[k]) in (int, float) and math.isfinite(value[k]) and value[k] > 0 for k in ('max_cost_usd', 'estimated_cost_usd', 'duration_hours')) and value['estimated_cost_usd'] <= value['max_cost_usd']
        good = good and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{2,127}', value['approval_id']) and isinstance(value['pricing_basis'], str) and 10 <= len(value['pricing_basis']) <= 2048 and value['data_policy'] == 'retain-filestore-network-disks'
        good = good and dt.datetime.fromisoformat(value['expires_at'].replace('Z', '+00:00')) > dt.datetime.now(dt.timezone.utc)
        ledger = value['ledger']
        good = good and ledger['currency'] == 'USD' and float(ledger['incremental_cost']) == value['estimated_cost_usd'] and float(ledger['limit']) == value['max_cost_usd']
    except (KeyError, ValueError, TypeError): good = False
    if not good: raise GcpError('GCP_BUDGET_APPROVAL_REQUIRED')
    if context:
        sys.path.insert(0, str(ROOT / 'infrastructure/providers/terraform_tools'))
        from provision import reserve_budget
        try: reserve_budget({'provider_kind': 'gcp', 'variables': {'project_id': config['project_id']}, 'budget': ledger}, {'operation_id': 'environment:' + context['environment_id']})
        except Exception as error: raise GcpError('GCP_BUDGET_LEDGER_BLOCKED') from error
    return {key: value[key] for key in ('approval_id', 'estimated_cost_usd', 'max_cost_usd', 'duration_hours', 'pricing_basis', 'data_policy')}


def validate_config(config):
    missing = [key for key in REQUIRED if not isinstance(config, dict) or not config.get(key)]
    if missing: return {'valid': False, 'missing': sorted(missing), 'errors': []}
    try: require_config(config)
    except Exception as error: return {'valid': False, 'missing': [], 'errors': [getattr(error, 'code', 'GCP_OPERATOR_INPUT_INVALID')]}
    return {'valid': True, 'missing': [], 'errors': [], 'live_verified': False}


def config_template(directory):
    root = Path(directory)
    adapter = {'project_id': '', 'project_number': '', 'auth': {'mode': 'workload_identity', 'credentials_file': str(root / 'gcp-wif.json'), 'service_account': ''},
        'boot_image': '', 'ssh_identity_file': '', 'ssh_public_key': '', 'zones': ['asia-northeast3-a', 'asia-northeast3-b', 'asia-northeast3-c'],
        'subnet_cidr': '10.76.0.0/24', 'filestore_cidr': '10.77.0.0/26', 'filestore_capacity_gib': 1024, 'machine_type': 'e2-standard-4', 'data_disk_gib': 100,
        'management_source_cidrs': [], 'observer_source_cidrs': [], 'edge_variables_file': str(root / 'gcp-edge.json'), 'dns_config_file': str(root / 'gcp-dns.json'),
        'application_template_file': str(root / 'application-template.json'), 'budget_file': str(root / 'gcp-budget.json'), 'database_config_file': ''}
    files = {'gcp-wif.json': {'type': 'external_account', 'audience': '', 'subject_token_type': 'urn:ietf:params:oauth:token-type:jwt', 'token_url': 'https://sts.googleapis.com/v1/token', 'service_account_impersonation_url': '', 'credential_source': {'file': str(root / 'gcp-subject-token.txt')}},
        'gcp-edge.json': {'hostname': '', 'node_port': 30080, 'health_path': '/healthz'},
        'gcp-dns.json': {'version': 1, 'zone_id': '', 'base_domain': '', 'token_file': '', 'state_dir': ''},
        'application-template.json': {'version': 1, 'cd': {'version': 1, 'state_dir': '', 'repository': '', 'branch': '', 'context': ''}, 'profile': {'provider': 'gcp', 'tenant': '', 'source_repository': '', 'pull_secret_file': '', 'target': {'repo_url': '', 'architecture': 'amd64', 'ingress_cidrs': [], 'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '512Mi'}}}, 'ingress': {'base_domain': '', 'observability_config_file': '', 'ci_credentials_file': ''}}},
        'gcp-budget.json': {'version': 1, 'approval_id': '', 'project_id': '', 'region': 'asia-northeast3', 'expires_at': '', 'node_count': None, 'machine_type': 'e2-standard-4', 'data_disk_gib': 100, 'filestore_capacity_gib': 1024, 'estimated_cost_usd': None, 'max_cost_usd': None, 'duration_hours': None, 'pricing_basis': '', 'data_policy': 'retain-filestore-network-disks', 'ledger': {'ledger_path': '', 'scope': '', 'currency': 'USD', 'incremental_cost': '', 'limit': '', 'unreported_cost': '', 'quoted_at': '', 'expires_at': ''}}}
    sys.path.insert(0, str(ROOT / 'infrastructure/ansible'))
    from standalone import config_template as postgres_template
    pg = postgres_template(root)['files']; pg['postgres-config.json']['backup'] = {'type': 'gcs', 'bucket': '', 'prefix': '/railshot/postgres', 'identity': 'instance', 'retention_full': 7}; files.update(pg)
    return {'adapter': adapter, 'files': files, 'notes': ['Populate private inputs; source tokens rotate in their file, never in chat.', 'Regional Filestore minimum 1024 GiB and retained network/disks must be included in the approved budget.', 'An empty generated approval is not authorization.']}
