"""Cross-module admission contracts; all fixtures and readiness calls are offline."""
import base64
import copy
from contextlib import contextmanager, redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / 'deployment/scripts')]
import api
import environment
import kubeadm
import run as ansible
import storage_profile


class Fixtures:
    """Private, synthetic snapshots/configuration; never use a user's cloud state."""
    def __init__(self, root, provider):
        self.root, self.provider = root, provider
        self.cluster = 'cluster-a'
        self.account = {'aws': '123456789012', 'gcp': 'railshot-project',
                        'openstack': '44444444-4444-4444-4444-444444444444'}[provider]
        self.region = {'aws': 'ap-northeast-2', 'gcp': 'asia-northeast3', 'openstack': 'RegionOne'}[provider]
        self.ssh = {'user': 'ubuntu', 'port': 22, 'identity_file': self.write('ssh-key', 'synthetic-key', raw=True),
                    'known_hosts_file': self.write('known-hosts', 'synthetic-host-key', raw=True)}
        self.credentials = self.write('cloud-credentials.json', self.auth())
        storage = json.loads((HERE / 'examples/storage' / (provider + '.json')).read_text())['runtime']['storage']
        storage['binding'].update(cluster_id=self.cluster, account_id=self.account, region=self.region)
        if provider != 'openstack':
            storage.update(state_dir=str(root / 'storage-state'), credentials_file=self.credentials)
        members, workers = self.members()
        edge_config = json.loads((ROOT / f'infrastructure/terraform/{provider}-edge/cluster-edge-config.json.example').read_text())
        edge_config.update(cluster_id=self.cluster, account_id=self.account, region=self.region, credentials_file=self.credentials)
        values = json.loads((ROOT / f'infrastructure/terraform/{provider}-edge/cluster-vars.json.example').read_text())
        edge_config['variables_file'] = self.write('edge-values.json', values)
        edge_config['worker_bindings'] = {node['resource_id']: self.edge_binding(index) for index, node in enumerate(workers, 2)}
        if provider != 'aws':
            token = self.write('dns-token', 'synthetic-dns-token', raw=True)
            edge_config['dns_config_file'] = self.write('dns.json', {'version': 1, 'zone_id': 'a' * 32,
                'base_domain': 'example.com', 'token_file': token, 'state_dir': str(root / 'dns-state')})
        if provider == 'openstack':
            tunnel = json.loads((ROOT / 'infrastructure/terraform/openstack-edge/cluster-tunnel.json.example').read_text())
            tunnel.update(cluster_id=self.cluster, origin_vip=values['vip_address'])
            tunnel.pop('ca_configmap', None); tunnel.pop('ca_file', None)
            tunnel['credentials_file'] = self.write('tunnel-credentials.json', {'AccountTag': 'b' * 32,
                'TunnelID': tunnel['tunnel_id'], 'TunnelSecret': base64.b64encode(b'x' * 32).decode()})
            application = 'app-' + hashlib.sha256(json.dumps([self.cluster, 'demo', 'web'], ensure_ascii=False,
                                                              separators=(',', ':')).encode()).hexdigest()[:24]
            edge_config['tunnel'] = {'config_file': self.write('tunnel.json', tunnel), 'request': {
                'environment_id': self.cluster, 'application_id': application, 'app': 'web',
                'tenant': 'demo', 'hostname': 'web.example.com'}}
        self.edge = edge_config
        self.edge_path = self.write('edge.json', edge_config)
        self.config = {'version': 1, 'cluster_id': self.cluster, 'provider': provider, 'account_id': self.account,
                       'region': self.region, 'members': members, 'runtime': {'wait_timeout_seconds': 300,
                                                                           'storage': storage, 'edge': self.edge_path}}
        self.cluster_path = self.write('cluster.json', self.config)
        self.registry = self.write('targets.json', {'version': 1, 'targets': {self.cluster: {
            'profile': kubeadm.PROFILE, 'purpose': 'runtime', 'cluster_file': self.cluster_path}}})

    def write(self, name, value, *, raw=False):
        path = self.root / name
        path.write_text(value if raw else json.dumps(value)); path.chmod(0o600)
        return str(path)

    def auth(self):
        if self.provider == 'aws':
            return {'AWS_ACCESS_KEY_ID': 'synthetic-access', 'AWS_SECRET_ACCESS_KEY': 'synthetic-secret'}
        if self.provider == 'gcp':
            return {'type': 'service_account', 'project_id': self.account, 'client_email': 'synthetic@railshot-project.iam.gserviceaccount.com',
                    'private_key': '-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----\n',
                    'token_uri': 'https://oauth2.googleapis.com/token'}
        return {'OS_AUTH_URL': 'https://keystone.example.com/v3', 'OS_PROJECT_ID': self.account,
                'OS_REGION_NAME': self.region, 'OS_APPLICATION_CREDENTIAL_ID': 'synthetic-app',
                'OS_APPLICATION_CREDENTIAL_SECRET': 'synthetic-secret'}

    def members(self):
        members, workers = [], []
        for index, name in enumerate(('control', 'compute-a', 'compute-b'), 1):
            zone = self.region + ('-' if self.provider == 'gcp' else '') + chr(96 + index) if self.provider != 'openstack' else 'nova-' + chr(96 + index)
            node = {'id': name, 'private_ipv4': f'10.20.{index}.10', 'availability_zone': zone,
                    'resource_id': f'arn:aws:ec2:{self.region}:{self.account}:instance/i-{index:017x}'}
            member = {'id': name, 'role': 'control_plane' if index == 1 else 'workers',
                      'availability_zone': zone, 'ssh': copy.deepcopy(self.ssh)}
            if self.provider in ('aws', 'gcp'):
                transport = f'ssm:{self.region}:i-{index:017x}'
                if self.provider == 'gcp':
                    node['resource_id'] = f'projects/{self.account}/zones/{zone}/instances/{name}'
                    transport = f'iap:{self.account}/{zone}/{name}'
                descriptor = {'schema_version': 'v1', 'execution_driver': 'terraform', 'provider_kind': self.provider,
                    'architecture': 'x86_64', 'target_id': name, 'resource_id': node['resource_id'], 'transport_ref': transport,
                    'addresses': {'private': node['private_ipv4']}, 'location': {'region': self.region, 'zone': zone}}
                member['descriptor_file'] = self.write(name + '-descriptor.json', descriptor)
            else:
                node['resource_id'] = f'{index}' * 8 + '-' + f'{index}' * 4 + '-' + f'{index}' * 4 + '-' + f'{index}' * 4 + '-' + f'{index}' * 12
                server = {'id': node['resource_id'], 'name': name, 'project_id': self.account, 'status': 'ACTIVE',
                          'availability_zone': zone, 'region': self.region,
                          'addresses': [{'network': 'management', 'address': node['private_ipv4'], 'version': 4}]}
                member.update(server_file=self.write(name + '-server.json', server), resource_id=node['resource_id'],
                              management_network='management')
            members.append(member)
            if index != 1: workers.append(node)
        return members, workers

    def edge_binding(self, index):
        return {'aws': {'security_group_id': f'sg-{index:017x}'}, 'gcp': {},
                'openstack': {'subnet_id': f'{index + 3}' * 8 + '-' + f'{index + 3}' * 4 + '-' + f'{index + 3}' * 4 + '-' + f'{index + 3}' * 4 + '-' + f'{index + 3}' * 12}}[self.provider]

    def request(self, identity='contract.1'):
        return kubeadm.from_registry(copy.deepcopy(self.config), request_id=identity)


class AWSNative:
    """Only the Terraform process boundary is simulated; edge admission/state stays real."""
    def __init__(self, edge, request):
        self.edge, self.request, self.calls, self.applied = edge, request, [], False
        self.resources = [('aws_lb', 'app', 'owned-lb'),
                          ('aws_lb_target_group_attachment', 'app', 'owned-worker-a'),
                          ('aws_lb_target_group_attachment', 'compute', 'owned-worker-b')]

    def __call__(self, argv, *, cwd, timeout):
        self.calls.append(argv)
        operation = argv[1]
        if operation == 'plan':
            self.edge.durable_write(cwd / argv[-1].split('=', 1)[1], b'offline exact saved plan')
        if operation == 'show':
            return self.edge.encoded({'resource_changes': [{'address': kind + '.' + name,
                'mode': 'managed', 'type': kind, 'change': {'actions': ['no-op'] if self.applied else ['create'],
                'before': {'id': identity} if self.applied else None, 'after': {'id': identity}}}
                for kind, name, identity in self.resources]})
        if operation == 'apply':
            self.applied = True
            self.edge.durable_write(cwd / 'terraform.tfstate', self.edge.encoded({'version': 4,
                'serial': 1, 'lineage': 'contract-only', 'resources': [{'mode': 'managed', 'type': kind,
                    'name': name, 'instances': [{'attributes': {'id': identity}}]}
                    for kind, name, identity in self.resources]}))
        if operation == 'output':
            configured = self.edge.validate(self.request)
            return self.edge.encoded({key: {'value': value} for key, value in {
                'alb_dns_name': 'synthetic.ap-northeast-2.elb.amazonaws.com',
                'target_group_arns': {name: 'arn:fixture-target-group/' + name for name in configured['values']['routes']},
                'certificate_arn': configured['values']['certificate_arn']}.items()})
        return b''


class IntegrationContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import cluster_edge
        cls.edge = cluster_edge

    @contextmanager
    def fixture(self, provider):
        with tempfile.TemporaryDirectory(prefix='railshot-contract-') as directory:
            root = Path(directory); root.chmod(0o700)
            yield Fixtures(root, provider)

    def test_all_provider_registry_validation_preview_and_api_use_real_validators(self):
        for provider in ('aws', 'gcp', 'openstack'):
            with self.subTest(provider=provider), self.fixture(provider) as fixture, \
                    patch.object(subprocess, 'Popen', side_effect=AssertionError('contract admission cannot execute native commands')), \
                    patch.object(ansible, 'forwarded_port', side_effect=AssertionError('contract admission cannot SSH')):
                request = fixture.request()
                self.assertIs(ansible.validate(request), request)
                self.assertIs(storage_profile.validate(request), request)
                edge = self.edge.validate(request)
                self.assertEqual(edge['fixed_routes'][0]['node_port'], 30080)
                preview = ansible.run(request, validate_only=True)
                self.assertEqual(preview['status'], 'validated')
                stdout = StringIO()
                with redirect_stdout(stdout):
                    code = ansible.main(['--cluster-file', fixture.cluster_path, '--request-id', 'contract.cli', '--validate-only'])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout.getvalue())['status'], 'validated')
                self.assertFalse(any(value for key, value in preview.items() if key.endswith('_ready') or key == 'public_http_verified'))
                jobs = api.Jobs(fixture.registry, fixture.root / 'api-state')
                try:
                    body = {'request_id': 'contract.api', 'target_id': fixture.cluster, 'operation': 'runtime.install',
                            'parameters': {'wait_timeout_seconds': 500}}
                    actual = jobs.prepare(body)
                    self.assertEqual(actual['runtime']['wait_timeout_seconds'], 500)
                    self.assertEqual(len(actual['inventory']['control_plane']), 1)
                    self.assertEqual(len(actual['inventory']['workers']), 2)
                    public = json.dumps(jobs.preview(body))
                    self.assertNotIn(str(fixture.root), public)
                    self.assertNotIn('synthetic-secret', public)
                finally: jobs.close()

    def test_storage_and_edge_scope_mismatches_reject_before_native_calls(self):
        for provider in ('aws', 'gcp', 'openstack'):
            with self.subTest(provider=provider), self.fixture(provider) as fixture, \
                    patch.object(subprocess, 'Popen', side_effect=AssertionError('invalid scope cannot execute')):
                fixture.config['runtime']['storage']['binding']['account_id'] = 'different'
                with self.assertRaises(ValueError): fixture.request()
                fixture.config['runtime']['storage']['binding']['account_id'] = fixture.account
                fixture.edge['account_id'] = 'different'; fixture.write('edge.json', fixture.edge)
                with self.assertRaises(ValueError): fixture.request()
                fixture.edge['account_id'] = fixture.account; fixture.write('edge.json', fixture.edge)
                node = fixture.config['members'][-1]
                node['availability_zone'] = 'unverified-zone'
                with self.assertRaises(ValueError): fixture.request()

    def test_environment_registration_matches_actual_fixed_route_output(self):
        for provider in ('aws', 'gcp', 'openstack'):
            with self.subTest(provider=provider), self.fixture(provider) as fixture:
                request = fixture.request()
                app = {'target': {'node_port': 30080}, 'public_http': {'url': 'https://web.example.com/health'}}
                self.assertEqual(environment.fixed_cluster_route(request, app), self.edge.validate(request)['binding_sha256'])
                for field, value in [('node_port', 30081), ('url', 'https://other.example.com/health'), ('url', 'https://web.example.com/other')]:
                    changed = copy.deepcopy(app)
                    changed['target' if field == 'node_port' else 'public_http'][field] = value
                    with self.assertRaisesRegex(ValueError, 'KUBEADM_FIXED_EDGE_ROUTE_MISMATCH'):
                        environment.fixed_cluster_route(request, changed)

    def test_registration_entry_uses_fixed_edge_binding_and_rejects_legacy_writer_before_claim(self):
        for provider in ('aws', 'gcp', 'openstack'):
            with self.subTest(provider=provider), self.fixture(provider) as fixture, \
                    patch.object(subprocess, 'Popen', side_effect=AssertionError('registration admission cannot execute')):
                pull_file = fixture.write('pull.json', {'auths': {'ghcr.io': {
                    'auth': base64.b64encode(b'reader:synthetic-pull-secret').decode()}}})
                config = {'version': 1, 'registration': {'state_dir': str(fixture.root / 'registrations'),
                    'source_repository': 'owner/source', 'pull_secret_file': pull_file},
                    'cd': {'version': 1, 'state_dir': str(fixture.root / 'cd-state'),
                        'repository': str(fixture.root / 'config-repo'), 'context': 'control', 'branch': 'deployment/apps',
                        'targets': {fixture.cluster: {'app': 'web', 'tenant': 'demo', 'target': {
                            'id': fixture.cluster, 'namespace': 'app-web', 'argocd_namespace': 'argocd',
                            'project': 'railshot-web', 'architecture': 'amd64',
                            'path': 'gitops/applications/web/' + fixture.cluster,
                            'repo_url': 'https://github.com/owner/config', 'node_port': 30080,
                            'image_pull_secret': {'namespace': 'app-web', 'name': 'ghcr-pull'}},
                            'public_http': {'url': 'https://web.example.com/health', 'expected_json': {'ok': True}}}}}}
                config_file = fixture.write('registration.json', config)
                request, cd, _, _, _, identity = environment.load(fixture.registry, fixture.cluster, config_file)
                self.assertEqual(identity['cluster_edge_sha256'], self.edge.validate(request)['binding_sha256'])
                self.assertEqual(cd['targets'][fixture.cluster]['target']['cluster_server'], 'https://10.20.1.10:6443')
                config['registration']['edge_config_file'] = '/private/legacy-edge.json'
                fixture.write('registration.json', config)
                with self.assertRaisesRegex(ValueError, 'KUBEADM_LEGACY_EDGE_WRITER_UNSUPPORTED'):
                    environment.register(fixture.registry, fixture.cluster, config_file, fixture.root / 'registration-home')
                self.assertFalse((fixture.root / 'registration-home').exists())
                self.assertFalse((fixture.root / 'registrations').exists())

    def test_actual_storage_cli_receipt_is_accepted_by_core_and_partial_proof_is_rejected(self):
        from test_storage_profile import installer, ProofKube
        with self.fixture('gcp') as fixture:
            request = fixture.request(); normalized = storage_profile.variables(request)['railshot_storage']
            normalized['backend_descriptor'] = {**normalized['binding'], 'backend': 'filestore', 'server': '10.0.8.2', 'share': '/apps'}
            target = ProofKube(normalized)
            original = installer.install
            config = fixture.write('storage-config.json', {'railshot_storage': normalized, 'request_id': request['request_id'],
                'target_id': request['target']['id'], 'nonce': 'a' * 32, 'wait_seconds': 300})
            proof = fixture.root / 'storage-receipt.json'
            def real_install(storage, **kwargs):
                return original(storage, **kwargs, kube=target, helm_runner=lambda *a: None, downloader=lambda *a: None)
            with patch.object(installer, 'install', side_effect=real_install), \
                    patch.object(sys, 'argv', ['install.py', '--config', config, '--receipt', str(proof)]):
                installer.main()
            observed = kubeadm.receipt(proof, request, 'storage', 'a' * 32)
            self.assertTrue(observed['storage_ready'])
            self.assertEqual(sum(doc['kind'] == 'PersistentVolumeClaim' for doc in target.applied), 1)
            observed['storage_pvc_ready'] = False; proof.write_text(json.dumps(observed))
            with self.assertRaises(ValueError): kubeadm.receipt(proof, request, 'storage', 'a' * 32)

    def test_orchestration_passes_sanitized_env_and_preflight_edge_binding(self):
        with self.fixture('aws') as fixture:
            request = fixture.request()
            binding = self.edge.validate(request)['binding_sha256']
            native = AWSNative(self.edge, request)
            edge_receipts, active_transports = [], set()
            @contextmanager
            def forwarding(reference, deadline):
                active_transports.add(reference)
                try: yield 40222
                finally: active_transports.remove(reference)
            def play(argv, *args):
                self.assertEqual(len(active_transports), 3)
                if argv[0] == 'ssh': return 0
                variables = json.loads(Path(argv[-1][1:]).read_text())
                stage = Path(argv[3]).stem.removeprefix('kubeadm-')
                ready = {stage + '_ready': True, 'node_ids': [n['id'] for n in variables['railshot_expected_nodes']]}
                if stage == 'storage': ready = {key: True for key in ('storage_backend_ready', 'storage_csi_ready', 'storage_pvc_ready', 'storage_ready')}
                Path(variables['railshot_receipt_path']).write_text(json.dumps({**ready, 'request_id': request['request_id'],
                    'target_id': request['target']['id'], 'nonce': variables['railshot_nonce'], 'stage': stage}))
                return 0
            def backend(*args, **kwargs): return {'storage_backend_ready': True, 'variables': storage_profile.variables(request)}
            def edge(*args, **kwargs):
                self.assertEqual(len(active_transports), 3)
                self.assertEqual(kwargs['expected_binding_sha256'], binding)
                self.assertEqual(kwargs['env'], ansible.child_env())
                self.assertNotIn('AWS_SECRET_ACCESS_KEY', kwargs['env'])
                observed = self.edge.provision(*args, **kwargs, runner=native)
                edge_receipts.append(observed)
                return observed
            with patch.object(kubeadm.shutil, 'which', return_value='/offline/ansible-playbook'), \
                    patch.object(ansible, 'forwarded_port', side_effect=forwarding), \
                    patch.object(subprocess, 'Popen', side_effect=AssertionError('offline executor cannot execute native commands')):
                result = kubeadm.run(request, state_dir=fixture.root / 'native-state', runner=play,
                                     storage_prepare=backend, edge_provision=edge)
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(active_transports, set())
            self.assertFalse(result['public_http_verified'])
            self.assertEqual(sum(call[1] == 'apply' for call in native.calls), 1)
            self.assertEqual(edge_receipts[0]['binding_sha256'], binding)
            self.assertTrue(edge_receipts[0]['configured'])
            for key in ('lb_healthy', 'dns_resolved', 'tls_verified', 'public_http_verified'):
                self.assertFalse(edge_receipts[0][key])

    def test_edge_private_credential_change_rejects_before_stage_creation_or_commands(self):
        with self.fixture('aws') as fixture:
            request = fixture.request()
            binding = self.edge.validate(request)['binding_sha256']
            fixture.write('cloud-credentials.json', {**fixture.auth(), 'AWS_SECRET_ACCESS_KEY': 'changed-synthetic-secret'})
            self.assertNotEqual(self.edge.validate(request)['binding_sha256'], binding)
            with patch.object(subprocess, 'Popen', side_effect=AssertionError('changed authority cannot execute')), \
                    self.assertRaisesRegex(self.edge.EdgeError, 'CLUSTER_EDGE_SCOPE_CHANGED'):
                self.edge.provision(request, state_dir=fixture.root / 'edge-state', expected_binding_sha256=binding,
                    runner=lambda *a, **kw: self.fail('changed authority cannot start a command'))
            self.assertFalse((fixture.root / 'edge-state').exists())


if __name__ == '__main__': unittest.main()
