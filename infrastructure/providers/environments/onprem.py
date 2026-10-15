"""Project-bound Neutron/Nova environment adapter using private operator profiles.

All native mutations have durable intent records. A lost response is never a
reason to create a second resource. Credentials and raw provider errors do not
leave this adapter; Kubernetes readiness comes from the existing executor.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import importlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

from .contract import ProviderError

ROOT = Path(__file__).resolve().parents[3]
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
LABEL = re.compile(r'[a-z][a-z0-9-]{0,62}')
READINESS = ('infrastructure', 'cluster', 'storage', 'public_access', 'database', 'deployment_binding', 'application_http')


def require(value, code='ONPREM_CONFIGURATION_INVALID'):
    if not value:
        raise ProviderError(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def private_file(value, *, json_value=True):
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path, 'ONPREM_PRIVATE_PATH_INVALID')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and info.st_nlink == 1 and not info.st_mode & 0o077
                and 0 < info.st_size <= 1_000_000, 'ONPREM_PRIVATE_FILE_INVALID')
        raw = stream.read(1_000_001)
    return json.loads(raw) if json_value else raw


def private_directory(value):
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path, 'ONPREM_PRIVATE_PATH_INVALID')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and not info.st_mode & 0o077, 'ONPREM_STATE_DIRECTORY_INVALID')
    return path


def save(path, value):
    path = Path(path)
    require(not path.is_symlink(), 'ONPREM_PRIVATE_PATH_INVALID')
    fd, temporary = tempfile.mkstemp(prefix='.onprem-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def normalize(row):
    require(isinstance(row, dict), 'ONPREM_RESPONSE_INVALID')
    return {key.lower().replace(' ', '_'): value for key, value in row.items()}


def identifier(value):
    require(isinstance(value, str) and IDENT.fullmatch(value), 'ONPREM_IDENTIFIER_INVALID')
    return value


def render(value, substitutions):
    """Only whole string placeholders; never shell or arbitrary expression expansion."""
    if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
        require(value[2:-1] in substitutions, 'ONPREM_TEMPLATE_REFERENCE_INVALID')
        return copy.deepcopy(substitutions[value[2:-1]])
    if isinstance(value, dict):
        return {render(key, substitutions): render(child, substitutions) for key, child in value.items()}
    if isinstance(value, list):
        return [render(child, substitutions) for child in value]
    return value


def validate_config(config, *, check_files=True):
    """Offline setup report: field names and error codes only, never values."""
    missing, errors, references = [], [], []
    required = ('account_id', 'credential_ref', 'project_id', 'region', 'network.cidr',
        'network.external_network_id', 'network.management_network_id', 'network.management_cidr',
        'nodes.image_id', 'nodes.flavor_id', 'nodes.availability_zones', 'nodes.ssh.user',
        'nodes.ssh.identity_file', 'nodes.public_key_file', 'storage_template_file',
        'edge_template_file', 'application_template_file')
    def lookup(path):
        item = config
        for part in path.split('.'):
            if not isinstance(item, dict) or part not in item:
                return None
            item = item[part]
        return item
    for field in required:
        if lookup(field) in (None, '', []):
            missing.append(field)
    if not isinstance(config, dict):
        return {'valid': False, 'missing': list(required), 'errors': ['ONPREM_PROFILE_OBJECT_REQUIRED'], 'verification': 'configuration_only'}
    def placeholders(item, path=''):
        if isinstance(item, dict):
            for key, child in item.items(): placeholders(child, (path + '.' if path else '') + key)
        elif isinstance(item, list):
            for index, child in enumerate(item): placeholders(child, path + '[' + str(index) + ']')
        elif isinstance(item, str) and ('REPLACE_' in item or item == 'generated'):
            missing.append(path)
    placeholders(config)
    if bool(config.get('remote_config_file')) == bool(config.get('credentials_file')):
        errors.append('ONPREM_SELECT_ONE_CREDENTIAL_TRANSPORT')
    for field in ('account_id', 'credential_ref', 'project_id', 'region', 'nodes.image_id', 'nodes.flavor_id'):
        value = lookup(field)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}' if field in ('account_id', 'credential_ref') else IDENT.pattern, value)):
            errors.append('ONPREM_INVALID_' + field.upper().replace('.', '_'))
    for field in ('network.cidr', 'network.management_cidr'):
        value = lookup(field)
        if value is not None:
            try:
                network = ipaddress.IPv4Network(value, strict=True)
                require(network.prefixlen > 0 and (field != 'network.cidr' or network.is_private and 16 <= network.prefixlen <= 27))
            except (ValueError, TypeError, ProviderError):
                errors.append('ONPREM_INVALID_' + field.upper().replace('.', '_'))
    zones = lookup('nodes.availability_zones')
    if zones is not None and (not isinstance(zones, list) or len(zones) != 3
                             or any(not isinstance(z, str) or not IDENT.fullmatch(z) for z in zones)):
        errors.append('ONPREM_THREE_NODE_ZONES_REQUIRED')
    if lookup('nodes.ssh.user') == 'root':
        errors.append('ONPREM_NONROOT_GUEST_USER_REQUIRED')
    for field in ('credentials_file', 'remote_config_file', 'nodes.ssh.identity_file', 'nodes.ssh.known_hosts_file',
                  'nodes.public_key_file', 'storage_template_file', 'edge_template_file', 'application_template_file',
                  'database.config_file'):
        value = lookup(field)
        if value is not None:
            if not isinstance(value, str) or not Path(value).is_absolute() or '..' in Path(value).parts:
                errors.append('ONPREM_INVALID_' + field.upper().replace('.', '_'))
            else:
                references.append((field, value))
    if check_files:
        seen = set()
        schemas = {
            'credentials_file': ('auth_url', 'project_id', 'application_credential_id', 'application_credential_secret'),
            'remote_config_file': ('owner_id', 'account_id', 'project_id', 'personal_config', 'personal_state_dir', 'target_file'),
            'storage_template_file': ('backend', 'mode', 'cephfs'),
            'edge_template_file': ('variables_file', 'credentials_file', 'dns_config_file', 'tunnel'),
            'application_template_file': ('version', 'state_dir', 'registry_file', 'cd', 'environments'),
        }
        def inspect_file(field, value, depth=0):
            if value in seen:
                return
            seen.add(value)
            require(depth < 6, 'ONPREM_REFERENCE_DEPTH')
            is_json = field not in ('nodes.ssh.identity_file', 'nodes.ssh.known_hosts_file', 'nodes.public_key_file')
            content = private_file(value, json_value=is_json)
            if not is_json:
                return
            require(isinstance(content, dict) and content, 'ONPREM_CONFIG_OBJECT_REQUIRED')
            for key in schemas.get(field, ()):
                require(key in content and content[key] not in (None, '', []), 'ONPREM_NESTED_FIELDS_REQUIRED')
            if field == 'storage_template_file':
                ceph = content.get('cephfs', {})
                require(all(ceph.get(k) for k in ('cluster_id', 'monitors', 'fs_name', 'subvolume_group', 'secret_files')),
                        'ONPREM_STORAGE_FIELDS_REQUIRED')
            if field == 'edge_template_file':
                require(isinstance(content.get('tunnel'), dict) and content['tunnel'].get('config_file')
                    and isinstance(content['tunnel'].get('request'), dict)
                    and all(content['tunnel']['request'].get(k) for k in ('app', 'tenant', 'hostname')),
                    'ONPREM_EDGE_FIELDS_REQUIRED')
            if field == 'application_template_file':
                require(isinstance(content.get('environments'), dict) and content['environments'], 'ONPREM_APPLICATION_FIELDS_REQUIRED')
                for profile in content['environments'].values():
                    require(isinstance(profile, dict) and all(profile.get(k) for k in
                        ('provider', 'tenant', 'source_repository', 'pull_secret_file', 'target', 'ingress')),
                        'ONPREM_APPLICATION_FIELDS_REQUIRED')
            def walk(item, key=''):
                if isinstance(item, dict):
                    for child_key, child in item.items():
                        if child_key == 'secret_files':
                            require(isinstance(child, dict) and child, 'ONPREM_SECRET_REFERENCES_REQUIRED')
                            for reference in child.values(): inspect_file('secret_reference', reference, depth + 1)
                        else:
                            walk(child, child_key)
                elif isinstance(item, list):
                    for child in item: walk(child, key)
                elif isinstance(item, str):
                    require(bool(item) and 'REPLACE_' not in item and 'example.com' not in item,
                            'ONPREM_NESTED_INPUT_REQUIRED')
                    if (key.endswith('_file') or key == 'personal_config') and item != 'generated' and not item.startswith('${'):
                        inspect_file('nested_reference', item, depth + 1)
            walk(content)
        for field, value in references:
            try:
                inspect_file(field, value)
            except (OSError, ValueError, TypeError, ProviderError):
                errors.append('ONPREM_INVALID_REFERENCE_' + field.upper().replace('.', '_'))
    return {'valid': not missing and not errors, 'missing': sorted(missing), 'errors': sorted(set(errors)),
            'verification': 'configuration_only'}


def config_template(directory):
    """Nonsecret editable files for the common offline setup CLI."""
    directory = Path(directory)
    require(directory.is_absolute(), 'ONPREM_PRIVATE_PATH_INVALID')
    source = ROOT / 'docs/operations/onprem-environment'
    def replace(value):
        if isinstance(value, dict):
            return {key: replace(child) for key, child in value.items()}
        if isinstance(value, list):
            return [replace(child) for child in value]
        if isinstance(value, str):
            if 'REPLACE_' in value:
                return ''
            if value.startswith('/etc/railshot/private/onprem/'):
                return str(directory / value.rsplit('/', 1)[-1])
        return value
    files = {path.name.removesuffix('.example'): replace(json.loads(path.read_text())) for path in source.glob('*.json.example')}
    adapter = files.pop('profile.json')
    files['credentials.json'] = {'auth_url': '', 'project_id': '', 'application_credential_id': '', 'application_credential_secret': ''}
    files['edge-credentials.json'] = {'OS_AUTH_URL': '', 'OS_PROJECT_ID': '', 'OS_REGION_NAME': 'RegionOne',
        'OS_APPLICATION_CREDENTIAL_ID': '', 'OS_APPLICATION_CREDENTIAL_SECRET': '', 'OS_AUTH_TYPE': 'v3applicationcredential'}
    files['tunnel-credentials.json'] = {'AccountTag': '', 'TunnelSecret': '', 'TunnelID': ''}
    files['ceph-provisioner.json'] = {'adminID': '', 'adminKey': ''}
    files['ceph-node.json'] = {'userID': '', 'userKey': ''}
    return {'adapter': adapter, 'files': files, 'notes': [
        'Fill identifiers and secret references only in these executor-owned 0600 files; never send secret values in chat.',
        'remote_config_file uses the existing enrolled personal vault/gateway; use credentials_file instead only for an explicit direct operator profile.',
        'Guest management NICs require a verified SSH route from the Ansible executor; a gateway connection to the management host alone is insufficient.',
        'CephFS, Octavia/Barbican, DNS, Named Tunnel and standalone PostgreSQL backup configuration must be supplied before apply.',
        'Configuration validation is offline and is not proof of live readiness.']}


class RemoteCLI:
    """Native operations stay inside the enrolled customer credential boundary."""
    def __init__(self, config, context):
        self.config, self.context = config, context
        private_file(config['remote_config_file'])

    def run(self, argv, *, json_output=True):
        params = {'argv': list(argv)}
        if argv[:2] == ['keypair', 'create'] and '--public-key' in argv:
            at = params['argv'].index('--public-key')
            params['public_key'] = private_file(params['argv'][at + 1], json_value=False).decode().strip()
            del params['argv'][at:at + 2]
        if argv[:2] == ['server', 'create'] and '--user-data' in argv:
            at = params['argv'].index('--user-data')
            raw = private_file(params['argv'][at + 1], json_value=False).decode()
            require(raw.startswith('#cloud-config\n'), 'ONPREM_GUEST_INPUT_INVALID')
            cloud = json.loads(raw.split('\n', 1)[1])
            marker = cloud['runcmd'][0][2].split("'", 2)[1]
            params['guest'] = {'user': cloud['users'][0]['name'], 'public_key': cloud['users'][0]['ssh_authorized_keys'][0], 'marker': marker}
            del params['argv'][at:at + 2]
        if 'delete' in argv[:3]:
            params['delete_data'] = True
        payload = {'context': {key: self.context[key] for key in ('owner_id', 'account_id')}, 'params': params}
        try:
            result = subprocess.run(['node', str(ROOT / 'apps/api/src/onprem-provider-transport.js'), self.config['remote_config_file']],
                input=encoded(payload), capture_output=True, timeout=150, check=False)
            require(result.returncode == 0 and len(result.stdout) <= 1_000_000, 'ONPREM_REMOTE_EXECUTION_FAILED')
            response = json.loads(result.stdout)
            require(response.get('project_id') == self.config['project_id'], 'ONPREM_PROJECT_MISMATCH')
            return response['result'] if json_output else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise ProviderError('ONPREM_REMOTE_OUTCOME_UNKNOWN', outcome_unknown=True) from None

    def verify(self):
        self.run(['quota', 'show'])

    def host_key(self, server, marker):
        return self.run(['server', 'hostkey', server, marker.rstrip()]).get('public_key', '')


class Adapter:
    def __init__(self, config):
        self.config = copy.deepcopy(config)

    def _profile(self, context):
        cfg = self.config
        require(context['credential_ref'] == cfg.get('credential_ref'), 'ONPREM_CREDENTIAL_BINDING_MISMATCH')
        require(context['account_id'] == cfg.get('account_id'), 'ONPREM_ACCOUNT_MISMATCH')
        spec = context.get('spec')
        require(not spec or spec['region'] == cfg.get('region'), 'ONPREM_REGION_MISMATCH')
        for key in ('project_id', 'region'):
            identifier(cfg.get(key))
        require(not spec or LABEL.fullmatch(context['environment_id']), 'ONPREM_ENVIRONMENT_ID_INVALID')
        if not spec:
            return cfg
        nodes, network = cfg['nodes'], cfg['network']
        for key in ('image_id', 'flavor_id'):
            identifier(nodes[key])
        require(len(nodes['availability_zones']) == 3 and all(IDENT.fullmatch(z) for z in nodes['availability_zones']))
        ssh = nodes['ssh']
        require({'user', 'identity_file'} <= set(ssh) <= {'user', 'identity_file', 'known_hosts_file'} and ssh['user'] != 'root'
                and IDENT.fullmatch(ssh['user']), 'ONPREM_SSH_PROFILE_INVALID')
        private_file(ssh['identity_file'], json_value=False)
        if 'known_hosts_file' in ssh:
            private_file(ssh['known_hosts_file'], json_value=False)
        public = private_file(nodes['public_key_file'], json_value=False).decode().strip()
        require(re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', public), 'ONPREM_SSH_PUBLIC_KEY_INVALID')
        key_check = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', ssh['identity_file']],
            capture_output=True, text=True, timeout=30, check=False)
        require(key_check.returncode == 0 and key_check.stdout.split()[:2] == public.split()[:2], 'ONPREM_SSH_KEYPAIR_MISMATCH')
        cidr = ipaddress.IPv4Network(network['cidr'], strict=True)
        management = ipaddress.IPv4Network(network['management_cidr'], strict=True)
        require(cidr.is_private and not cidr.is_loopback and 16 <= cidr.prefixlen <= 27
                and management.prefixlen > 0, 'ONPREM_NETWORK_PROFILE_INVALID')
        identifier(network['external_network_id'])
        identifier(network['management_network_id'])
        for key in ('storage_template_file', 'edge_template_file', 'application_template_file'):
            private_file(cfg[key])
        edge = private_file(cfg['edge_template_file'])
        for key in ('variables_file', 'credentials_file', 'dns_config_file'):
            private_file(edge[key])
        private_file(edge['tunnel']['config_file'])
        if spec['database']['mode'] == 'standalone':
            require(isinstance(cfg.get('database'), dict), 'ONPREM_DATABASE_PROFILE_REQUIRED')
            identifier(cfg['database']['flavor_id'])
            private_file(cfg['database']['config_file'])
        return cfg

    def _cli(self, context):
        cfg = self._profile(context)
        if cfg.get('remote_config_file'):
            return RemoteCLI(cfg, context)
        from infrastructure.providers.openstack.cli import OpenStackCLI
        if cfg.get('vault'):
            sys.path.insert(0, str(ROOT / 'deployment/bootstrap'))
            from client_setup.credentials import AssistedCredentialStore
            vault = cfg['vault']
            unwrap = private_file(cfg['credentials_file'])
            require(set(unwrap) == {'unlock_key'}, 'ONPREM_VAULT_REFERENCE_INVALID')
            auth = AssistedCredentialStore(Path(vault['home'])).unlock(unwrap['unlock_key'],
                target_id=vault['target_id'], generation=vault['generation'], project_id=cfg['project_id'])
        else:
            auth = private_file(cfg['credentials_file'])
        require(isinstance(auth, dict) and auth.get('project_id') == cfg['project_id'], 'ONPREM_PROJECT_MISMATCH')
        auth = {**auth, 'region_name': cfg['region']}
        return OpenStackCLI(auth, timeout=60, temp_parent='/dev/shm')

    def _scope(self, cli):
        if isinstance(cli, RemoteCLI):
            cli.verify(); return
        token = normalize(cli.run(['token', 'issue']))
        require(token.get('project_id') == self.config['project_id'], 'ONPREM_PROJECT_MISMATCH')

    def _read(self, cli, kind, ident):
        row = normalize(cli.run([*kind.split(), 'show', identifier(ident)]))
        require(row.get('project_id', row.get('tenant_id')) == self.config['project_id']
                or kind == 'keypair', 'ONPREM_RESOURCE_PROJECT_MISMATCH')
        require(row.get('id', row.get('name')) == ident, 'ONPREM_RESOURCE_ID_MISMATCH')
        return row

    @contextmanager
    def _state(self, context):
        home = private_directory(str(Path(context['state_dir']) / 'onprem'))
        fd = os.open(home / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            binding = {key: context[key] for key in ('environment_id', 'owner_id', 'account_id', 'credential_ref', 'policy_revision')}
            binding['spec_sha256'] = hashlib.sha256(encoded(context['spec'])).hexdigest()
            binding['config_sha256'] = hashlib.sha256(encoded(self.config)).hexdigest()
            path = home / 'state.json'
            state = private_file(str(path)) if path.exists() else {'binding': binding, 'resources': [], 'steps': {},
                'readiness': {key: False for key in READINESS}, 'status': 'blocked'}
            require(state['binding'] == binding, 'ONPREM_STATE_BINDING_CHANGED')
            yield home, state
        finally:
            os.close(fd)

    def _once(self, cli, home, state, key, kind, argv, *, mutation=True, result=True):
        digest = hashlib.sha256(encoded(argv)).hexdigest()
        prior = state['steps'].get(key)
        if prior:
            require(prior['sha256'] == digest, 'ONPREM_OPERATION_CHANGED')
            require(prior['status'] == 'succeeded', 'ONPREM_RECONCILIATION_REQUIRED')
            return prior.get('result')
        state['steps'][key] = {'sha256': digest, 'status': 'unknown'}
        save(home / 'state.json', state)
        try:
            raw = cli.run(argv, json_output=result)
        except Exception:
            raise ProviderError('ONPREM_NATIVE_OUTCOME_UNKNOWN', outcome_unknown=mutation) from None
        safe = None
        if result:
            row = normalize(raw)
            ident = identifier(row.get('id', row.get('name') if kind == 'keypair' else None))
            safe = {'id': ident}
            state['resources'].append({'kind': kind, 'id': ident, 'status': 'created', 'stage': key})
        state['steps'][key].update(status='succeeded', result=safe)
        save(home / 'state.json', state)
        return safe

    def connection(self, context):
        cli = self._cli(context); self._scope(cli)
        return {'status': 'succeeded', 'connected': True, 'provider': 'openstack'}

    def catalog(self, context):
        cli = self._cli(context); self._scope(cli)
        return {'images': [{key: row.get(key) for key in ('id', 'name', 'status')} for row in
                           map(normalize, cli.run(['image', 'list']))],
                'sizes': [{key: row.get(key) for key in ('id', 'name', 'vcpus', 'ram', 'disk')} for row in
                          map(normalize, cli.run(['flavor', 'list']))],
                'zones': [normalize(row).get('zone_name') for row in cli.run(['availability', 'zone', 'list'])]}

    def plan(self, spec, context):
        cfg = self._profile(context)
        cli = self._cli(context); self._scope(cli)
        image = normalize(cli.run(['image', 'show', cfg['nodes']['image_id']]))
        require(str(image.get('status')).lower() == 'active' and (image.get('owner') == cfg['project_id']
                or image.get('visibility') == 'public'), 'ONPREM_IMAGE_UNAVAILABLE')
        flavor = normalize(cli.run(['flavor', 'show', cfg['nodes']['flavor_id']]))
        require(int(flavor.get('vcpus', 0)) >= 2 and int(flavor.get('ram', 0)) >= 2048
                and int(flavor.get('disk', 0)) >= 20, 'ONPREM_FLAVOR_TOO_SMALL')
        management = normalize(cli.run(['network', 'show', cfg['network']['management_network_id']]))
        require(management.get('project_id') == cfg['project_id'] or management.get('shared') is True, 'ONPREM_MANAGEMENT_NETWORK_MISMATCH')
        external = normalize(cli.run(['network', 'show', cfg['network']['external_network_id']]))
        require(external.get('router:external') is True, 'ONPREM_EXTERNAL_NETWORK_REQUIRED')
        return {'status': 'planned', 'provider': 'openstack', 'scope': spec['scope'],
                'vm_count': 4 if spec['database']['mode'] == 'standalone' else 3,
                'steps': ['network', 'subnet', 'router', 'security_groups', 'ports', 'servers',
                          'kubeadm', 'cephfs', 'public_access', 'database', 'applications'],
                'readiness': {key: False for key in READINESS}}

    def _wait(self, cli, kind, ident, desired, *, seconds=600):
        deadline = time.monotonic() + seconds
        while True:
            row = self._read(cli, kind, ident)
            if row.get('status') == desired:
                return row
            require(row.get('status') not in ('ERROR', 'error'), 'ONPREM_RESOURCE_FAILED')
            require(time.monotonic() < deadline, 'ONPREM_RESOURCE_TIMEOUT')
            time.sleep(3)

    def _provision(self, cli, home, state, context):
        cfg = self.config; name = context['environment_id']; network = cfg['network']; nodes = cfg['nodes']
        create = lambda key, kind, args: self._once(cli, home, state, key, kind, args)['id']
        net = create('network', 'network', ['network', 'create', '--description', 'Railshot ' + name, name])
        cidr = ipaddress.IPv4Network(network['cidr'])
        pool = 'start=' + str(cidr.network_address + 16) + ',end=' + str(cidr.broadcast_address - 1)
        subnet = create('subnet', 'subnet', ['subnet', 'create', '--network', net, '--subnet-range', network['cidr'],
                    '--ip-version', '4', '--allocation-pool', pool, name])
        router = create('router', 'router', ['router', 'create', '--description', 'Railshot ' + name, name])
        self._read(cli, 'network', net); self._read(cli, 'subnet', subnet); self._read(cli, 'router', router)
        self._once(cli, home, state, 'router-gateway', None, ['router', 'set', '--external-gateway', network['external_network_id'], router], result=False)
        self._once(cli, home, state, 'router-interface', None, ['router', 'add', 'subnet', router, subnet], result=False)
        group = create('security-group', 'security group', ['security', 'group', 'create', '--description', 'Railshot ' + name, name])
        self._read(cli, 'security group', group)
        # SSH/API only from operator management CIDR; cluster protocols only
        # among this environment's ports. No ingress from 0.0.0.0/0.
        for port in ('22', '6443'):
            create('management-' + port, 'security group rule', ['security', 'group', 'rule', 'create', '--ingress', '--ethertype', 'IPv4',
                    '--protocol', 'tcp', '--dst-port', port, '--remote-ip', network['management_cidr'], group])
        for protocol in ('tcp', 'udp', 'icmp'):
            create('cluster-' + protocol, 'security group rule', ['security', 'group', 'rule', 'create', '--ingress', '--ethertype', 'IPv4',
                    '--protocol', protocol, '--remote-group', group, group])
        key = create('keypair', 'keypair', ['keypair', 'create', '--public-key', nodes['public_key_file'], name])
        require(self._read(cli, 'keypair', key).get('public_key', '').split()[:2] ==
                private_file(nodes['public_key_file'], json_value=False).decode().split()[:2], 'ONPREM_KEYPAIR_CHANGED')
        members = []; snapshots = {}; ports = {}
        names = ['control', 'worker-a', 'worker-b'] + (['database'] if context['spec']['database']['mode'] == 'standalone' else [])
        for index, role in enumerate(names):
            vm_name = name + '-' + role
            port = create('port-' + role, 'port', ['port', 'create', '--network', net, '--security-group', group, vm_name])
            observed_port = self._read(cli, 'port', port)
            require(observed_port.get('network_id') == net and observed_port.get('security_group_ids', observed_port.get('security_groups')) == [group],
                    'ONPREM_PORT_BINDING_MISMATCH')
            management_port = create('management-port-' + role, 'port', ['port', 'create', '--network', network['management_network_id'], '--security-group', group, vm_name + '-management'])
            self._read(cli, 'port', management_port)
            zone = nodes['availability_zones'][min(index, 2)]
            flavor = cfg['database']['flavor_id'] if role == 'database' else nodes['flavor_id']
            marker = 'RAILSHOT_HOST_KEY:' + hashlib.sha256((name + ':' + role).encode()).hexdigest() + ' '
            cloud = {'users': [{'name': nodes['ssh']['user'], 'lock_passwd': True, 'shell': '/bin/bash', 'sudo': ['ALL=(ALL) NOPASSWD:ALL'],
                       'ssh_authorized_keys': [private_file(nodes['public_key_file'], json_value=False).decode().strip()]}],
                     'ssh_pwauth': False, 'disable_root': True,
                     'runcmd': [['sh', '-c', 'printf ' + repr(marker) + ' > /dev/console; cat /etc/ssh/ssh_host_ed25519_key.pub > /dev/console']]}
            cloud_path = home / (role + '-cloud-init.json')
            fd = os.open(cloud_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write('#cloud-config\n' + json.dumps(cloud))
            server = create('server-' + role, 'server', ['server', 'create', '--image', nodes['image_id'], '--flavor', flavor,
                '--nic', 'port-id=' + port, '--nic', 'port-id=' + management_port, '--key-name', key, '--availability-zone', zone,
                '--user-data', str(cloud_path),
                '--property', 'railshot.environment=' + name, '--property', 'railshot.owner=' + context['owner_id'], vm_name])
            row = self._wait(cli, 'server', server, 'ACTIVE')
            props = row.get('properties', {})
            require(isinstance(props, dict) and props.get('railshot.environment') == name
                    and props.get('railshot.owner') == context['owner_id'], 'ONPREM_SERVER_OWNER_MISMATCH')
            observed_port = self._read(cli, 'port', port)
            addresses = observed_port.get('fixed_ips', [])
            require(observed_port.get('device_id') == server and isinstance(addresses, list) and len(addresses) == 1
                    and addresses[0].get('subnet_id') == subnet, 'ONPREM_PORT_BINDING_MISMATCH')
            address = str(ipaddress.IPv4Address(addresses[0]['ip_address']))
            require(ipaddress.IPv4Address(address) in ipaddress.IPv4Network(network['cidr']), 'ONPREM_ADDRESS_MISMATCH')
            snapshot = {'id': server, 'project_id': cfg['project_id'], 'status': 'ACTIVE', 'region': cfg['region'],
                'availability_zone': zone, 'addresses': [{'network': net, 'address': address, 'version': 4}]}
            file = home / (role + '-server.json'); save(file, snapshot)
            management = self._read(cli, 'port', management_port)
            require(management.get('device_id') == server and management.get('network_id') == network['management_network_id']
                    and len(management.get('fixed_ips', [])) == 1, 'ONPREM_MANAGEMENT_PORT_MISMATCH')
            ssh = {**nodes['ssh'], 'connect_host': str(ipaddress.IPv4Address(management['fixed_ips'][0]['ip_address']))}
            if 'known_hosts_file' not in ssh:
                host_key = self._console_key(cli, server, marker, context)
                known = home / (role + '-known-hosts')
                content = address + ' ' + host_key + '\n'
                if known.exists():
                    require(private_file(str(known), json_value=False).decode() == content, 'ONPREM_HOST_KEY_CHANGED')
                else:
                    fd = os.open(known, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'w') as stream:
                        stream.write(content); stream.flush(); os.fsync(stream.fileno())
                ssh['known_hosts_file'] = str(known)
            snapshots[role] = snapshot; ports[role] = port
            state.setdefault('ssh', {})[role] = ssh
            if role != 'database':
                members.append({'id': role, 'role': 'control_plane' if role == 'control' else 'workers',
                    'availability_zone': zone, 'ssh': ssh, 'server_file': str(file), 'resource_id': server, 'management_network': net})
        state['readiness']['infrastructure'] = True
        state['network'] = {'network_id': net, 'subnet_id': subnet, 'router_id': router, 'security_group_id': group}
        state['snapshots'] = snapshots; state['ports'] = ports
        save(home / 'state.json', state)
        return members

    def _console_key(self, cli, server, marker, context):
        """Pin only the public host key emitted by this newly owned Nova guest."""
        from infrastructure.providers.openstack.cli import OpenStackCLI
        deadline = time.monotonic() + 600
        while True:
            row = self._read(cli, 'server', server)
            props = row.get('properties', {})
            require(props.get('railshot.environment') == context['environment_id']
                    and props.get('railshot.owner') == context['owner_id'], 'ONPREM_SERVER_OWNER_MISMATCH')
            if isinstance(cli, RemoteCLI):
                public = cli.host_key(server, marker)
                if public:
                    require(re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', public), 'ONPREM_HOST_KEY_INVALID')
                    return ' '.join(public.split()[:2])
                require(time.monotonic() < deadline, 'ONPREM_HOST_KEY_UNAVAILABLE')
                time.sleep(3); continue
            captured = []
            def capture(argv, **kwargs):
                result = cli.runner(argv, **kwargs)
                require(len(result.stdout or '') <= 1_000_000, 'ONPREM_CONSOLE_TOO_LARGE')
                captured.append(result.stdout or '')
                return result
            probe = OpenStackCLI(cli.auth, runner=capture, timeout=cli.timeout, temp_parent='/dev/shm')
            probe.run(['console', 'log', 'show', server, '--lines', '200'], json_output=False)
            keys = {line[len(marker):].strip() for line in ''.join(captured).splitlines() if line.startswith(marker)}
            require(len(keys) <= 1, 'ONPREM_HOST_KEY_CONFLICT')
            if keys:
                public = keys.pop()
                require(re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', public), 'ONPREM_HOST_KEY_INVALID')
                return ' '.join(public.split()[:2])
            require(time.monotonic() < deadline, 'ONPREM_HOST_KEY_UNAVAILABLE')
            time.sleep(3)

    def _runtime(self, home, state, context, members):
        cfg = self.config; name = context['environment_id']
        substitutions = {'environment_id': name, 'project_id': cfg['project_id'], 'region': cfg['region'],
                         'state_dir': str(home), **state['network']}
        for role, snapshot in state['snapshots'].items():
            substitutions[role + '_id'] = snapshot['id']; substitutions[role + '_address'] = snapshot['addresses'][0]['address']
            substitutions[role + '_port_id'] = state['ports'][role]
        edge = render(private_file(cfg['edge_template_file']), substitutions)
        edge.update(cluster_id=name, provider='openstack', account_id=cfg['project_id'], region=cfg['region'],
            worker_bindings={state['snapshots'][role]['id']: {'subnet_id': state['network']['subnet_id']} for role in ('worker-a', 'worker-b')})
        values = render(private_file(edge['variables_file']), substitutions)
        values.update(name=name, vip_subnet_id=state['network']['subnet_id'],
                      vip_address=str(ipaddress.IPv4Network(cfg['network']['cidr']).network_address + 10))
        values_path = home / 'edge-variables.json'; save(values_path, values); edge['variables_file'] = str(values_path)
        tunnel = render(private_file(edge['tunnel']['config_file']), substitutions)
        tunnel.update(cluster_id=name, origin_vip=values['vip_address'])
        tunnel_path = home / 'tunnel.json'; save(tunnel_path, tunnel); edge['tunnel']['config_file'] = str(tunnel_path)
        tr = edge['tunnel']['request']; tr['environment_id'] = name
        tr['application_id'] = 'app-' + hashlib.sha256(json.dumps([name, tr['tenant'], tr['app']], separators=(',', ':')).encode()).hexdigest()[:24]
        # File references in operator templates may use the whole environment
        # directory placeholder but cannot contain user-controlled expressions.
        edge_path = home / 'edge.json'; save(edge_path, edge)
        storage = render(private_file(cfg['storage_template_file']), substitutions)
        storage['binding'] = {'cluster_id': name, 'provider': 'openstack', 'account_id': cfg['project_id'], 'region': cfg['region']}
        cluster = {'version': 1, 'cluster_id': name, 'provider': 'openstack', 'account_id': cfg['project_id'],
                   'region': cfg['region'], 'members': members, 'runtime': {'wait_timeout_seconds': 600,
                        'storage': storage, 'edge': str(edge_path)}}
        cluster_path = home / 'cluster.json'; save(cluster_path, cluster)
        sys.path.insert(0, str(ROOT / 'infrastructure/ansible'))
        kubeadm = importlib.import_module('kubeadm')
        request = kubeadm.from_registry(cluster, request_id=context.get('operation_id') or 'observe-' + context['environment_id'])
        result = kubeadm.run(request, state_dir=str(home / 'runtime'))
        state['readiness'].update(cluster=result.get('runtime_ready') is True, storage=result.get('storage_ready') is True,
                                  public_access=result.get('edge_ready') is True)
        state['runtime_status'] = result.get('status')
        save(home / 'state.json', state)
        require(result.get('status') == 'succeeded', 'ONPREM_RUNTIME_NOT_READY')
        registry_path = home / 'registry.json'
        save(registry_path, {'version': 1, 'targets': {name: {'profile': 'kubeadm-1c2w', 'cluster_file': str(cluster_path), 'purpose': 'runtime'}}})
        substitutions['registry_file'] = str(registry_path)
        app = render(private_file(cfg['application_template_file']), substitutions)
        app['environments'][name]['ingress'].update(cluster_edge_config_file=str(edge_path),
            cluster_edge_state_dir=str(home / 'runtime' / 'cluster-edge' / name), cluster_file=str(cluster_path))
        app_path = home / 'applications.json'; save(app_path, app)
        sys.path.insert(0, str(ROOT / 'deployment/scripts'))
        applications = importlib.import_module('applications')
        applications.load_config(str(app_path))
        app_name = context['spec'].get('application_name', 'readiness-check')
        profile = app['environments'].get(name, {})
        app_id = applications.application_id(name, profile.get('tenant'), app_name)
        applications._inputs(app, {'environment_id': name, 'app': app_name, 'application_id': app_id})
        require(name in app['environments'], 'ONPREM_APPLICATION_BINDING_MISMATCH')
        state['application_config_path'] = str(app_path)
        # Only the common Node binding probe can confirm the application adapter.
        state['readiness']['deployment_binding'] = False
        state['application_sha256'] = hashlib.sha256(encoded(app)).hexdigest()
        self._observe_runtime(home, state, context, initial=True)
        from infrastructure.providers.environments.runtime import bootstrap
        binding = bootstrap(str(app_path), context)
        require(binding.get('status') == 'succeeded', 'ONPREM_APPLICATION_BOOTSTRAP_FAILED')
        state['application_binding_receipt_path'] = binding['application_binding_receipt_path']
        save(home / 'state.json', state)

    def _database(self, home, state, context, action='install'):
        if context['spec']['database']['mode'] == 'none':
            return
        node = state['snapshots']['database']
        descriptor = {'version': 1, 'provider': 'openstack', 'environment_id': context['environment_id'],
            'account_id': context['account_id'], 'project_id': self.config['project_id'], 'region': self.config['region'],
            'resource_id': node['id'], 'private_ipv4': node['addresses'][0]['address'], 'ssh': state['ssh']['database']}
        file = home / 'database-node.json'; save(file, descriptor)
        # The shared standalone runtime owns PostgreSQL, backup and restore.
        sys.path.insert(0, str(ROOT / 'infrastructure/ansible'))
        standalone = importlib.import_module('standalone')
        result = standalone.run(action=action, descriptor=descriptor,
            config=private_file(self.config['database']['config_file']), context=context)
        state['readiness']['database'] = result.get('ready') is True
        state['database_result'] = {key: result[key] for key in ('status', 'ready', 'binding_file') if key in result}
        save(home / 'state.json', state)

    def apply(self, spec, context):
        self.plan(spec, context)
        cli = self._cli(context); self._scope(cli)
        with self._state(context) as (home, state):
            members = self._provision(cli, home, state, context)
            self._runtime(home, state, context, members)
            self._database(home, state, context)
            state['status'] = 'succeeded'
            save(home / 'state.json', state)
            return self._result(state)

    def _result(self, state):
        result = {key: copy.deepcopy(state[key]) for key in ('status', 'readiness', 'resources')}
        for key in ('application_config_path', 'application_binding_receipt_path', 'residuals', 'retained_resources'):
            if state.get(key):
                result[key] = copy.deepcopy(state[key])
        return result

    def _observe_runtime(self, home, state, context, *, initial=False):
        sys.path.insert(0, str(ROOT / 'infrastructure/ansible'))
        kubeadm = importlib.import_module('kubeadm')
        checker = importlib.import_module('kubeadm_readiness')
        request = kubeadm.from_registry(private_file(str(home / 'cluster.json')), request_id=context.get('operation_id') or 'observe-' + context['environment_id'])
        deadline = time.monotonic() + 600
        kube = kubeadm.kubectl(request, {}, kubeadm.ansible.child_env(), deadline)
        roster = kube('default', 'get', 'nodes', '-o', 'json')
        checker.check_nodes(roster, {'nodes': kubeadm.nodes(request), 'version': 'v' + kubeadm.VERSIONS['kubernetes'],
            'workers': [node['id'] for node in request['inventory']['workers']]})
        uid = kube('default', 'get', 'namespace', 'kube-system', '-o', 'json')['metadata']['uid']
        pvc = kube('railshot-storage-proof', 'get', 'pvc', 'shared-filesystem-proof', '-o', 'json')
        pv = kube('default', 'get', 'pv', pvc['spec']['volumeName'], '-o', 'json')
        identity = {'cluster_uid': uid, 'pvc_uid': pvc['metadata']['uid'], 'pv_uid': pv['metadata']['uid']}
        require(initial or state.get('runtime_identity') == identity, 'ONPREM_RUNTIME_IDENTITY_CHANGED')
        require(pvc['status']['phase'] == 'Bound' and pvc['spec']['storageClassName'] == 'railshot-persistent'
                and pv['spec']['claimRef']['uid'] == pvc['metadata']['uid']
                and pv['spec']['csi']['volumeAttributes'].get('clusterID') == request['runtime']['storage']['cephfs']['cluster_id']
                and pv['spec']['csi']['volumeAttributes'].get('fsName') == request['runtime']['storage']['cephfs']['fs_name'],
                'ONPREM_STORAGE_BINDING_CHANGED')
        workloads = kube('railshot-storage', 'get', 'deployment,daemonset', '-o', 'json')['items']
        require(workloads and all(row['status'].get('observedGeneration', 0) >= row['metadata']['generation']
            and (row['status'].get('readyReplicas', 0) == row['spec'].get('replicas', 1) if row['kind'] == 'Deployment'
                 else row['status'].get('numberReady', 0) == row['status'].get('desiredNumberScheduled', -1))
            for row in workloads), 'ONPREM_STORAGE_WORKLOAD_NOT_READY')
        app = private_file(state['application_config_path'])
        require(hashlib.sha256(encoded(app)).hexdigest() == state['application_sha256'], 'ONPREM_APPLICATION_POLICY_CHANGED')
        sys.path.insert(0, str(ROOT / 'deployment/scripts'))
        apps = importlib.import_module('applications')
        apps.load_config(state['application_config_path'])
        name = context['spec'].get('application_name', 'readiness-check')
        apps._inputs(app, {'environment_id': context['environment_id'], 'app': name,
            'application_id': apps.application_id(context['environment_id'], app['environments'][context['environment_id']]['tenant'], name)})
        state['runtime_identity'] = identity
        state['readiness'].update(cluster=True, storage=True, deployment_binding=False)
        if not initial:
            # provision's successful replay branch performs only native/DNS/Tunnel
            # readback. Refuse to enter it if the persisted stage is not configured.
            edge_dir = home / 'runtime' / 'cluster-edge'
            journal = private_file(str(edge_dir / context['environment_id'] / 'journal.json'))
            require(journal.get('phase') == 'configured', 'ONPREM_EDGE_RECONCILIATION_REQUIRED')
            edge = importlib.import_module('cluster_edge').provision(request, state_dir=edge_dir, deadline=deadline, kube=kube)
            state['readiness']['public_access'] = edge.get('edge_ready') is True

    def describe(self, context):
        cli = self._cli(context); self._scope(cli)
        with self._state(context) as (home, state):
            active = bool(state.get('snapshots'))
            for role, snapshot in state.get('snapshots', {}).items():
                row = self._read(cli, 'server', snapshot['id'])
                active = active and row.get('status') == 'ACTIVE'
                props = row.get('properties', {})
                require(props.get('railshot.environment') == context['environment_id']
                        and props.get('railshot.owner') == context['owner_id'], 'ONPREM_SERVER_OWNER_MISMATCH')
            state['readiness'] = {key: False for key in READINESS}
            state['readiness']['infrastructure'] = active
            state['status'] = 'blocked'
            if active and state.get('application_config_path'):
                try:
                    self._observe_runtime(home, state, context)
                    self._database(home, state, context, 'status')
                    state['status'] = 'succeeded'
                except (OSError, ValueError, KeyError, ProviderError):
                    state['status'] = 'blocked'
            save(home / 'state.json', state)
            return self._result(state)

    def operate(self, operation, context):
        action = operation.get('action') if isinstance(operation, dict) else operation
        if action in ('backup', 'restore'):
            with self._state(context) as (home, state):
                require(state.get('snapshots', {}).get('database'), 'ONPREM_DATABASE_NOT_PROVISIONED')
                self._database(home, state, context, action)
                return self._result(state)
        if action in ('refresh', 'verify'):
            return self.describe(context)
        if action in ('start', 'stop'):
            cli = self._cli(context); self._scope(cli)
            with self._state(context) as (home, state):
                require(state.get('snapshots'), 'ONPREM_NOT_PROVISIONED')
                roles = ['database', 'control', 'worker-a', 'worker-b']
                if action == 'stop':
                    roles.reverse()
                desired = 'ACTIVE' if action == 'start' else 'SHUTOFF'
                for role in roles:
                    node = state['snapshots'].get(role)
                    if not node:
                        continue
                    row = self._read(cli, 'server', node['id'])
                    props = row.get('properties', {})
                    require(props.get('railshot.environment') == context['environment_id']
                        and props.get('railshot.owner') == context['owner_id'], 'ONPREM_SERVER_OWNER_MISMATCH')
                    if row.get('status') != desired:
                        require(row.get('status') == ('SHUTOFF' if action == 'start' else 'ACTIVE'),
                                'ONPREM_SERVER_TRANSITION_UNSAFE')
                        self._once(cli, home, state, str(context['operation_id']) + '-' + action + '-' + role,
                            None, ['server', action, node['id']], result=False)
                    self._wait(cli, 'server', node['id'], desired)
                state['readiness'] = {key: False for key in READINESS}
                state['readiness']['infrastructure'] = action == 'start'
                state['status'] = 'succeeded'
                state['power_state'] = desired
                save(home / 'state.json', state)
                return self._result(state)
        raise ProviderError('ONPREM_OPERATION_UNSUPPORTED')

    def _absent(self, cli, resource):
        # A successful empty scoped list is evidence of absence; transport/auth
        # exceptions never mean that a resource was deleted.
        rows = cli.run([*resource['kind'].split(), 'list'])
        require(isinstance(rows, list), 'ONPREM_DELETE_READBACK_INVALID')
        return not any(normalize(row).get('id', normalize(row).get('name')) == resource['id'] for row in rows)

    def delete(self, context):
        cli = self._cli(context); self._scope(cli)
        with self._state(context) as (home, state):
            require(state.get('application_config_path'), 'ONPREM_RUNTIME_REVOCATION_REQUIRED')
            from infrastructure.providers.environments.runtime import teardown
            receipt = teardown(state['application_config_path'], context)
            require(receipt.get('status') == 'succeeded' and receipt.get('environment_id') == context['environment_id']
                    and receipt.get('data_policy') == 'retain', 'ONPREM_RUNTIME_REVOCATION_REQUIRED')
            state['teardown_receipt'] = receipt
            # PostgreSQL data currently resides on its VM root disk. Preserve
            # that VM and its entire network dependency chain until the shared
            # DB runtime supplies an explicit retained-data receipt.
            retain_database = 'database' in state.get('snapshots', {})
            if retain_database:
                database = state['snapshots']['database']
                observed = self._read(cli, 'server', database['id'])
                props = observed.get('properties', {})
                require(props.get('railshot.environment') == context['environment_id']
                    and props.get('railshot.owner') == context['owner_id'], 'ONPREM_SERVER_OWNER_MISMATCH')
                if observed.get('status') != 'SHUTOFF':
                    require(observed.get('status') == 'ACTIVE', 'ONPREM_SERVER_TRANSITION_UNSAFE')
                    self._once(cli, home, state, 'retain-database-stop', None,
                        ['server', 'stop', database['id']], result=False)
                self._wait(cli, 'server', database['id'], 'SHUTOFF')
            retained = []
            for resource in reversed(state['resources']):
                stage, kind = resource['stage'], resource['kind']
                if retain_database and not any(stage == prefix + role
                    for prefix in ('server-', 'port-', 'management-port-')
                    for role in ('control', 'worker-a', 'worker-b')):
                    resource['status'] = 'retained'; retained.append(copy.deepcopy(resource)); continue
                if resource.get('status') == 'deleted':
                    require(self._absent(cli, resource), 'ONPREM_DELETED_RESOURCE_REAPPEARED'); continue
                if self._absent(cli, resource):
                    resource['status'] = 'deleted'; save(home / 'state.json', state); continue
                observed = self._read(cli, kind, resource['id'])
                if kind == 'server':
                    props = observed.get('properties', {})
                    require(props.get('railshot.environment') == context['environment_id']
                        and props.get('railshot.owner') == context['owner_id'], 'ONPREM_SERVER_OWNER_MISMATCH')
                    require(not observed.get('volumes_attached', observed.get('os_extended_volumes:volumes_attached', [])),
                            'ONPREM_ATTACHED_VOLUME_REQUIRES_RETENTION')
                if kind == 'router':
                    self._once(cli, home, state, 'delete-router-interface', None,
                        ['router', 'remove', 'subnet', resource['id'], state['network']['subnet_id']], result=False)
                self._once(cli, home, state, 'delete-' + stage, None,
                    [*kind.split(), 'delete', resource['id']], result=False)
                deadline = time.monotonic() + 600
                while not self._absent(cli, resource):
                    require(time.monotonic() < deadline, 'ONPREM_DELETE_READBACK_TIMEOUT')
                    time.sleep(3)
                resource['status'] = 'deleted'; save(home / 'state.json', state)
            state['retained_resources'] = retained + receipt.get('retained_resources', [])
            state['residuals'] = []
            state['readiness'] = {key: False for key in READINESS}
            state['status'] = 'succeeded'
            save(home / 'state.json', state)
            return self._result(state)
