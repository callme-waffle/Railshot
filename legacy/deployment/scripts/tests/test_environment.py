"""No-cloud regression for registration ownership, resume, native RBAC and CI consumption."""
import base64
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
import environment as env


class Kube:
    def __init__(self):
        self.objects = {}
        self.applications = 0

    def __call__(self, namespace, *args, document=None):
        if args[:3] == ('get', 'services', '-A'):
            return {'items': [v for (_, kind, _), v in self.objects.items() if kind == 'service']}
        if args[:2] == ('config', 'view'):
            return {'clusters': [{'cluster': {'certificate-authority-data': base64.b64encode(b'-----BEGIN CERTIFICATE-----\nfake').decode()}}]}
        if args[:2] == ('create', '--raw'):
            claims = {'sub': 'system:serviceaccount:' + namespace + ':' + env.SA, 'aud': ['https://kubernetes.default.svc'],
                'exp': int(time.time()) + 21600, 'iat': int(time.time()),
                'kubernetes.io': {'namespace': namespace, 'serviceaccount': {'name': env.SA, 'uid': '12345678-1234-1234-1234-123456789012'}}}
            token = 'e30.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.signature'
            return {'status': {'token': token, 'expirationTimestamp': '2026-10-03T00:00:00Z'}}
        if args[0] in ('apply', 'replace'):
            data = copy.deepcopy(document); data['metadata'].setdefault('uid', '12345678-1234-1234-1234-123456789012')
            data['metadata']['resourceVersion'] = str(self.applications + 2)
            self.objects[namespace, data['kind'].lower(), data['metadata']['name']] = data
            self.applications += 1
            return data
        if args[0] == 'patch':
            key = (namespace, args[1].lower(), args[2])
            value = copy.deepcopy(self.objects[key])
            for operation in document:
                keys = [part.replace('~1', '/').replace('~0', '~') for part in operation['path'].split('/')[1:]]
                parent = value
                for part in keys[:-1]:
                    parent = parent[part]
                if operation['op'] == 'test':
                    if parent[keys[-1]] != operation['value']:
                        raise ValueError('fixture CAS changed')
                else:
                    assert operation['op'] == 'replace'
                    parent[keys[-1]] = operation['value']
            value['metadata']['resourceVersion'] = str(self.applications + 2)
            self.objects[key] = value
            self.applications += 1
            return copy.deepcopy(value)
        if args[0] == 'get':
            key = (namespace, args[1].lower(), args[2])
            value = self.objects.get(key)
            if value is None and '--ignore-not-found' not in args:
                raise ValueError('missing fixture resource ' + str(key))
            return copy.deepcopy(value)
        raise AssertionError(args)


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'environment-new'
        self.shared = self.root / 'registrations'
        self.target = 'stack-aws-1002'
        self.descriptor = json.loads((ROOT / 'examples/ansible/aws-node-descriptor.json').read_text())
        self.descriptor['target_id'] = self.target
        self.write('descriptor.json', self.descriptor)
        self.write('key', 'synthetic-key', raw=True); self.write('hosts', '10.77.0.10 ssh-ed25519 synthetic', raw=True)
        self.registry = {'version': 1, 'targets': {self.target: {'descriptor_file': str(self.root / 'descriptor.json'),
            'purpose': 'runtime', 'ssh': {'user': 'ubuntu', 'identity_file': str(self.root / 'key'), 'known_hosts_file': str(self.root / 'hosts')}}}}
        self.config = {'version': 1, 'registration': {'state_dir': str(self.shared), 'source_repository': 'owner/source',
            'pull_secret_file': str(self.root / 'pull.json')}, 'cd': {'version': 1, 'state_dir': str(self.root / 'cd-state'),
            'repository': str(self.root / 'config-repo'), 'context': 'control', 'branch': 'deployment/apps',
            'targets': {self.target: {'app': 'new-app', 'tenant': 'demo', 'target': {'id': self.target,
                'namespace': 'app-new', 'argocd_namespace': 'argocd', 'project': 'railshot-new', 'architecture': 'amd64',
                'path': 'gitops/applications/new-app/' + self.target, 'repo_url': 'https://github.com/owner/config',
                'node_port': 30085, 'image_pull_secret': {'namespace': 'app-new', 'name': 'ghcr-pull'}},
                'public_http': {'url': 'https://new.example.test/health', 'expected_json': {'ok': True}}}}}}
        self.write('pull.json', {'auths': {'ghcr.io': {'auth': base64.b64encode(b'reader:synthetic-secret').decode()}}})
        self.write('registry.json', self.registry); self.write('config.json', self.config)
        self.runtime, self.control = Kube(), Kube()
        self.bootstrap = yaml.safe_load((ROOT / 'deployment/manifests/runtime-registration-access.yaml').read_text())
        for document in self.bootstrap['items']:
            document['metadata']['resourceVersion'] = '1'
            self.control.objects[document['metadata']['namespace'], document['kind'].lower(), document['metadata']['name']] = document
        self.control.objects['argocd', 'cronjob', 'railshot-credentials'] = {'spec': {'jobTemplate': {'spec': {'template': {'spec': {
            'serviceAccountName': 'railshot-credentials', 'containers': [{'command': ['python3', '/app/gitops/credentials.py', 'renew']}]}}}}}}
        old = {'secret': 'railshot-old-target', 'target_id': 'old-target', 'server': 'https://10.0.0.2:6443', 'project': 'old-project',
            'namespaces': ['old-app'], 'service_account': {'name': env.SA, 'namespace': 'old-app', 'uid': '12345678-1234-1234-1234-123456789012'},
            'ca_sha256': 'a' * 64, 'audiences': ['https://kubernetes.default.svc']}
        self.control.objects['argocd', 'configmap', 'railshot-credentials'] = {'apiVersion': 'v1', 'kind': 'ConfigMap',
            'metadata': {'name': 'railshot-credentials'}, 'data': {'policy.json': json.dumps({'version': 1, 'targets': [old]}), 'kubeconfig': 'preserve-me'}}
        self.control.objects['argocd', 'role', 'railshot-credentials'] = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role',
            'metadata': {'name': 'railshot-credentials'}, 'rules': [{'apiGroups': [''], 'resources': ['secrets'],
            'resourceNames': ['railshot-old-target'], 'verbs': ['get', 'patch']}]}
        self.variable = None
        @contextmanager
        def runtime(_request):
            yield self.runtime
        self.enterContext(patch.object(env, 'runtime_kubectl', runtime))
        self.enterContext(patch.object(env.argo, 'kubectl', lambda context, *a, **kw: self.control(*a, **kw)))
        self.enterContext(patch.object(env.credentials, 'customer', side_effect=lambda server, ca, token, path, doc, **_kw:
            {'status': {'allowed': doc['spec']['resourceAttributes']['namespace'] == 'app-new'
                and doc['spec']['resourceAttributes']['resource'] == 'deployments'}}))
        self.enterContext(patch.object(env, 'github_variable', self.github))

    def write(self, name, value, raw=False):
        p = self.root / name
        p.write_text(value if raw else json.dumps(value)); p.chmod(0o600)
        return p

    def github(self, repo, name, document=None, **_kw):
        if document:
            self.variable = copy.deepcopy(document)
        return copy.deepcopy(self.variable)

    def run_registration(self):
        return env.register(self.root / 'registry.json', self.target, self.root / 'config.json', self.home)

    def fill_renewal_policy(self, count):
        cm = self.control.objects['argocd', 'configmap', 'railshot-credentials']
        original = json.loads(cm['data']['policy.json'])['targets'][0]
        targets = [copy.deepcopy(original)]
        for index in range(1, count):
            targets.append({**copy.deepcopy(original), 'target_id': f'reserved-{index}', 'secret': f'railshot-reserved-{index}'})
        cm['data']['policy.json'] = json.dumps({'version': 1, 'targets': targets})
        return targets

    def test_legacy_registration_checks_policy_size_before_external_writes(self):
        self.fill_renewal_policy(20)
        with patch.object(env.credentials, 'MAX_POLICY_BYTES', 1), self.assertRaises(env.credentials.PolicyCapacityError):
            self.run_registration()
        self.assertEqual((self.runtime.applications, self.control.applications), (0, 0))
        self.assertIsNone(self.variable)

    def test_install_renewal_rejects_oversized_or_invalid_candidate_before_role_write(self):
        targets = self.fill_renewal_policy(20)
        before = copy.deepcopy(self.control.objects)
        candidate = {**targets[0], 'target_id': 'new-target', 'secret': 'railshot-new-target'}
        limit = len(json.dumps({'version': 1, 'targets': targets}).encode()) + 1
        with patch.object(env.credentials, 'MAX_POLICY_BYTES', limit), self.assertRaises(env.credentials.PolicyCapacityError):
            env.install_renewal(self.config['cd'], candidate)
        self.assertEqual(self.control.applications, 0)
        self.assertEqual(self.control.objects, before)
        # Reinstalling an identical existing binding consumes no extra capacity.
        env.install_renewal(self.config['cd'], targets[0])
        self.assertEqual(self.control.applications, 0)
        self.fill_renewal_policy(1)
        candidate.pop('audiences')
        before = copy.deepcopy(self.control.objects)
        with self.assertRaisesRegex(ValueError, 'invalid registration binding'):
            env.install_renewal(self.config['cd'], candidate)
        self.assertEqual(self.control.applications, 0)
        self.assertEqual(self.control.objects, before)

    def use_openstack(self):
        self.server = {'id': 'server-1', 'project_id': 'project-1', 'status': 'ACTIVE',
            'addresses': [{'network': 'management', 'address': '10.26.1.5', 'version': 4}]}
        selected = self.registry['targets'][self.target]
        selected.pop('descriptor_file')
        selected.update(server_file=str(self.root / 'server.json'), resource_id='server-1', project_id='project-1',
            management_network='management', placement='onprem-a', architecture='amd64', initialization='cloud-init')
        self.write('server.json', self.server); self.write('registry.json', self.registry)

    def test_openstack_registration_binds_verified_server_without_terraform_descriptor(self):
        self.use_openstack()
        request, cd, _, _, _, identity = env.load(self.root / 'registry.json', self.target, self.root / 'config.json')
        self.assertEqual(request['target']['provider'], 'openstack')
        self.assertNotIn('transport_ref', request['inventory']['control_plane'][0]['ssh'])
        self.assertEqual(cd['targets'][self.target]['target']['cluster_server'], 'https://10.26.1.5:6443')
        self.assertEqual(identity['descriptor']['resource_id'], 'server-1')
        self.assertEqual(identity['descriptor']['openstack_server'], self.server)
        self.assertNotIn('execution_driver', identity['descriptor'])
        self.assertNotIn('schema_version', identity['descriptor'])
        result = self.run_registration()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['provider_kind'], 'openstack')
        self.assertTrue(result['deployment_supported'])
        self.assertEqual(self.run_registration(), result)
        self.registry['targets'][self.target]['ssh']['port'] = 2222
        self.write('registry.json', self.registry)
        with self.assertRaisesRegex(Exception, 'TARGET_REGISTRATION_CONFLICT'):
            self.run_registration()

    def test_openstack_unverified_or_ambiguous_server_never_registers(self):
        self.use_openstack()
        for changed in ({'id': 'other'}, {'project_id': 'other'}, {'status': 'BUILD'}, {'addresses': []},
                {'addresses': [{'network': 'other', 'address': '10.26.1.5', 'version': 4}]},
                {'addresses': [{'network': 'management', 'address': '203.0.113.5', 'version': 4}]},
                {'addresses': self.server['addresses'] * 2}):
            with self.subTest(changed=changed):
                self.write('server.json', {**self.server, **changed})
                with self.assertRaises(ValueError):
                    self.run_registration()
                self.assertFalse(self.shared.exists())
                self.assertEqual(self.control.applications, 0)

    def test_openstack_registry_cannot_mix_sources_or_override_cluster_address(self):
        self.use_openstack()
        self.registry['targets'][self.target]['descriptor_file'] = str(self.root / 'descriptor.json')
        self.write('registry.json', self.registry)
        with self.assertRaisesRegex(ValueError, 'OpenStack registry fields'):
            self.run_registration()
        del self.registry['targets'][self.target]['descriptor_file']
        self.write('registry.json', self.registry)
        self.config['cd']['targets'][self.target]['target']['cluster_server'] = 'https://10.26.1.6:6443'
        self.write('config.json', self.config)
        with self.assertRaisesRegex(ValueError, 'cluster endpoint'):
            self.run_registration()
        self.assertFalse(self.shared.exists())

    def test_openstack_unreachable_kubernetes_api_never_enables_deployment(self):
        self.use_openstack()
        with patch.object(env.credentials, 'customer', side_effect=OSError('unreachable API')):
            result = self.run_registration()
        self.assertEqual((result['status'], result['stage'], result['deployment_supported']), ('unknown', 'argo', False))
        self.assertFalse((self.home / 'cd.json').exists())
        self.assertIsNone(self.variable)

    def test_openstack_arm64_cannot_register_as_an_amd64_deployment_target(self):
        self.use_openstack()
        self.registry['targets'][self.target]['architecture'] = 'arm64'
        self.write('registry.json', self.registry)
        with self.assertRaises(ValueError):
            self.run_registration()
        self.assertFalse(self.shared.exists())
        self.assertIsNone(self.variable)

    def test_aws_and_gcp_descriptor_metadata_cannot_override_management_endpoint(self):
        for provider in ('aws', 'gcp'):
            with self.subTest(provider=provider):
                descriptor = json.loads((ROOT / f'examples/ansible/{provider}-node-descriptor.json').read_text())
                descriptor.update(target_id=self.target, management_endpoint='https://10.99.0.1:16443')
                descriptor['addresses']['metrics'] = '8.8.8.8'
                self.write('descriptor.json', descriptor)
                _, cd, _, _, _, identity = env.load(self.root / 'registry.json', self.target, self.root / 'config.json')
                self.assertEqual(cd['targets'][self.target]['target']['cluster_server'],
                    'https://' + descriptor['addresses']['private'] + ':6443')
                self.assertNotIn('metrics', identity['descriptor']['addresses'])

    def test_gcp_public_management_route_binds_allocated_ip_and_preserves_tls_identity(self):
        descriptor = json.loads((ROOT / 'examples/ansible/gcp-node-descriptor.json').read_text())
        descriptor['target_id'] = self.target
        descriptor['addresses']['public'] = '34.47.68.21'
        self.write('descriptor.json', descriptor)
        selected = self.registry['targets'][self.target]
        for endpoint in (None, 'http://34.47.68.21:6443', 'https://34.47.68.22:6443',
                'https://34.47.68.21:443', 'https://34.47.68.21:6443/api'):
            selected['management_endpoint'] = endpoint
            self.write('registry.json', self.registry)
            with self.assertRaises(ValueError):
                self.run_registration()
            self.assertFalse(self.shared.exists())
        for public in ('224.0.0.1', '239.1.2.3'):
            descriptor['addresses']['public'] = public
            self.write('descriptor.json', descriptor)
            selected['management_endpoint'] = f'https://{public}:6443'
            self.write('registry.json', self.registry)
            with self.assertRaises(ValueError):
                env.load(self.root / 'registry.json', self.target, self.root / 'config.json')
            self.assertFalse(self.shared.exists())
        descriptor['addresses']['public'] = '34.47.68.21'
        self.write('descriptor.json', descriptor)
        selected['management_endpoint'] = 'https://34.47.68.21:6443'
        self.write('registry.json', self.registry)
        descriptor['addresses']['metrics'] = '8.8.8.8'
        self.write('descriptor.json', descriptor)
        *_, identity = env.load(self.root / 'registry.json', self.target, self.root / 'config.json')
        self.assertEqual(identity['descriptor']['addresses']['metrics'], '34.47.68.21')
        result = self.run_registration()
        self.assertEqual(result['status'], 'succeeded', result)
        renewal = json.loads((self.home / 'renewal.json').read_text())
        self.assertEqual(renewal['server'], selected['management_endpoint'])
        self.assertEqual(renewal['tls_server_name'], descriptor['addresses']['private'])
        secret = self.control.objects['argocd', 'secret', 'railshot-' + self.target]
        tls = json.loads(base64.b64decode(secret['data']['config']))['tlsClientConfig']
        self.assertEqual(tls['serverName'], descriptor['addresses']['private'])
        self.assertIs(tls['insecure'], False)

    def test_gcp_public_management_route_binds_allocated_ip_and_preserves_tls_identity(self):
        descriptor = json.loads((ROOT / 'examples/ansible/gcp-node-descriptor.json').read_text())
        descriptor['target_id'] = self.target
        descriptor['addresses']['public'] = '34.47.68.21'
        self.write('descriptor.json', descriptor)
        selected = self.registry['targets'][self.target]
        for endpoint in (None, 'http://34.47.68.21:6443', 'https://34.47.68.22:6443',
                'https://34.47.68.21:443', 'https://34.47.68.21:6443/api'):
            selected['management_endpoint'] = endpoint
            self.write('registry.json', self.registry)
            with self.assertRaises(ValueError):
                self.run_registration()
            self.assertFalse(self.shared.exists())
        for public in ('224.0.0.1', '239.1.2.3'):
            descriptor['addresses']['public'] = public
            self.write('descriptor.json', descriptor)
            selected['management_endpoint'] = f'https://{public}:6443'
            self.write('registry.json', self.registry)
            with self.assertRaises(ValueError):
                env.load(self.root / 'registry.json', self.target, self.root / 'config.json')
            self.assertFalse(self.shared.exists())
        descriptor['addresses']['public'] = '34.47.68.21'
        self.write('descriptor.json', descriptor)
        selected['management_endpoint'] = 'https://34.47.68.21:6443'
        self.write('registry.json', self.registry)
        result = self.run_registration()
        self.assertEqual(result['status'], 'succeeded', result)
        renewal = json.loads((self.home / 'renewal.json').read_text())
        self.assertEqual(renewal['server'], selected['management_endpoint'])
        self.assertEqual(renewal['tls_server_name'], descriptor['addresses']['private'])
        secret = self.control.objects['argocd', 'secret', 'railshot-' + self.target]
        tls = json.loads(base64.b64decode(secret['data']['config']))['tlsClientConfig']
        self.assertEqual(tls['serverName'], descriptor['addresses']['private'])
        self.assertIs(tls['insecure'], False)

    def test_openstack_management_endpoint_is_explicit_and_bound_to_the_connection_host(self):
        self.use_openstack()
        selected = self.registry['targets'][self.target]
        selected['management_endpoint'] = 'https://10.26.1.5:16443'
        self.write('registry.json', self.registry)
        _, cd, _, _, _, identity = env.load(self.root / 'registry.json', self.target, self.root / 'config.json')
        self.assertEqual(cd['targets'][self.target]['target']['cluster_server'], selected['management_endpoint'])
        self.assertEqual(identity['descriptor']['addresses']['private'], '10.26.1.5')
        for endpoint in (None, 'http://10.26.1.5:16443', 'https://10.26.1.6:16443',
                'https://10.26.1.5', 'https://10.26.1.5:0', 'https://10.26.1.5:65536',
                'https://user@10.26.1.5:16443', 'https://10.26.1.5:16443/api',
                'https://10.26.1.5:16443?token=x', 'https://10.26.1.5:16443#x'):
            with self.subTest(endpoint=endpoint):
                selected['management_endpoint'] = endpoint
                self.write('registry.json', self.registry)
                with self.assertRaises(ValueError):
                    self.run_registration()
                self.assertFalse(self.shared.exists())

    def test_openstack_relay_registration_preserves_vm_identity_and_verifies_api_access(self):
        self.use_openstack()
        selected = self.registry['targets'][self.target]
        selected['ssh'].update(connect_host='172.31.0.172', port=10022)
        selected['management_endpoint'] = 'https://172.31.0.172:16443'
        self.write('registry.json', self.registry)
        request, cd, _, _, _, identity = env.load(self.root / 'registry.json', self.target, self.root / 'config.json')
        self.assertEqual(request['inventory']['control_plane'][0]['private_ipv4'], '10.26.1.5')
        self.assertEqual(identity['descriptor']['resource_id'], 'server-1')
        self.assertEqual(identity['descriptor']['addresses'], {'private': '10.26.1.5', 'metrics': '172.31.0.172'})
        self.assertEqual(cd['targets'][self.target]['target']['cluster_server'], 'https://172.31.0.172:16443')
        with patch.object(env.credentials, 'customer', side_effect=OSError('relay API unreachable')) as customer:
            result = self.run_registration()
        self.assertEqual(customer.call_args.args[0], selected['management_endpoint'])
        self.assertEqual(customer.call_args.kwargs, {'server_name': '10.26.1.5'})
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['deployment_supported'])
        self.assertIsNone(self.variable)
        result = self.run_registration()
        self.assertEqual(result['status'], 'succeeded', result)
        registered = env.bridge.read_config(self.home / 'cd.json')['targets'][self.target]['target']
        self.assertEqual(registered['cluster_server'], selected['management_endpoint'])
        renewal = json.loads((self.home / 'renewal.json').read_text())
        self.assertEqual(renewal['tls_server_name'], '10.26.1.5')
        secret = self.control.objects['argocd', 'secret', 'railshot-' + self.target]
        tls = json.loads(base64.b64decode(secret['data']['config']))['tlsClientConfig']
        self.assertEqual(tls['serverName'], '10.26.1.5')
        self.assertIs(tls['insecure'], False)

    def test_registration_is_consumed_replay_preserves_existing_and_conflict_blocks(self):
        result = self.run_registration()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertTrue(result['deployment_supported'])
        self.assertEqual(result['steps'], ['permissions', 'edge', 'namespace', 'argo', 'credentials', 'ci'])
        config = env.bridge.read_config(self.home / 'cd.json')
        self.assertEqual(config['targets'][self.target]['target']['cluster_server'], 'https://10.77.0.10:6443')
        self.assertEqual((self.home / 'cd.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('synthetic-secret', json.dumps(result))
        self.assertEqual(self.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['kubeconfig'], 'preserve-me')
        policy = json.loads(self.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json'])
        self.assertEqual([p['target_id'] for p in policy['targets']], ['old-target', self.target])
        selected = env.validate_target({'CONFIGURED_BINDINGS': self.variable['value'], 'TARGET_ID': self.target, 'APP': 'new-app', 'TENANT': 'demo'})
        self.assertEqual(selected['image_pull_secret']['namespace'], 'app-new')
        count = (self.runtime.applications, self.control.applications)
        self.assertEqual(self.run_registration(), result)
        self.assertEqual(count, (self.runtime.applications, self.control.applications))
        rules = self.control.objects['argocd', 'role', 'railshot-product-registrations']['rules']
        self.assertEqual(next(r['resourceNames'] for r in rules if r['resources'] == ['secrets']), ['railshot-' + self.target])
        self.assertEqual(next(r['resourceNames'] for r in rules if r['resources'] == ['applications']),
                         [env.application_name(self.target, 'app-new', 'new-app')])
        self.config['cd']['targets'][self.target]['app'] = 'other-app'
        self.config['cd']['targets'][self.target]['target']['path'] = 'gitops/applications/other-app/' + self.target
        self.write('config.json', self.config)
        with self.assertRaisesRegex(Exception, 'TARGET_REGISTRATION_CONFLICT'):
            self.run_registration()

    def test_partial_failure_persists_and_same_identity_resumes_without_extra_objects(self):
        with patch.object(env, 'bind_ci', side_effect=RuntimeError('synthetic-secret')):
            failed = self.run_registration()
        self.assertEqual(failed['status'], 'unknown')
        self.assertEqual(failed['stage'], 'ci')
        self.assertFalse((self.home / 'cd.json').exists())
        self.assertNotIn('synthetic-secret', (self.home / 'registration.json').read_text())
        identities = set(self.runtime.objects) | set(self.control.objects)
        result = self.run_registration()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertNotIn('error', result)
        self.assertEqual(identities, set(self.runtime.objects) | set(self.control.objects))

    def test_bootstrap_grants_registration_and_renewal_without_unrelated_access(self):
        subject = {'kind': 'ServiceAccount', 'name': 'railshot-product', 'namespace': 'railshot-system'}
        self.assertEqual((self.bootstrap['apiVersion'], self.bootstrap['kind']), ('v1', 'List'))
        self.assertEqual(len(self.bootstrap['items']), 5)
        for document in self.bootstrap['items']:
            self.assertIn(document['kind'], ('ServiceAccount', 'Role', 'RoleBinding'))
            if document['kind'] == 'ServiceAccount':
                self.assertEqual(document['metadata']['name'], subject['name'])
                self.assertEqual(document['metadata']['namespace'], subject['namespace'])
                self.assertFalse(document['automountServiceAccountToken'])
                continue
            self.assertEqual(document['metadata']['namespace'], 'argocd')
            if document['kind'] == 'RoleBinding':
                self.assertEqual(document['subjects'], [subject])
                self.assertEqual(document['roleRef'], {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role',
                    'name': document['metadata']['name']})
            for rule in document.get('rules', []):
                self.assertNotIn('*', sum(rule.values(), []))
                if rule['verbs'] != ['create']:
                    self.assertTrue(rule.get('resourceNames'))

        def allowed(group, resource, verb, name, namespace='argocd'):
            roles = [self.control.objects['argocd', 'role', item['roleRef']['name']]
                     for item in self.bootstrap['items'] if item['kind'] == 'RoleBinding']
            return namespace == 'argocd' and any(group in r['apiGroups'] and resource in r['resources']
                and verb in r['verbs'] and (not r.get('resourceNames') or name in r['resourceNames'])
                for role in roles for r in role.get('rules', []))

        for name in ('railshot-product-registrations', 'railshot-credentials'):
            for verb in ('get', 'update', 'escalate'):
                self.assertTrue(allowed('rbac.authorization.k8s.io', 'roles', verb, name))
        for verb in ('get', 'update'):
            self.assertTrue(allowed('', 'configmaps', verb, 'railshot-credentials'))
        self.assertTrue(allowed('batch', 'cronjobs', 'get', 'railshot-credentials'))
        for group, resource in (('', 'secrets'), ('argoproj.io', 'appprojects'), ('argoproj.io', 'applications')):
            self.assertTrue(allowed(group, resource, 'create', ''))
            self.assertFalse(allowed(group, resource, 'get', 'unrelated'))
            self.assertFalse(allowed(group, resource, 'list', ''))
            self.assertFalse(allowed(group, resource, 'create', '', 'kube-system'))
        for group, resource, verb, name in (
            ('rbac.authorization.k8s.io', 'roles', 'update', 'railshot-product-registration-bootstrap'),
            ('rbac.authorization.k8s.io', 'roles', 'escalate', 'unrelated'),
            ('rbac.authorization.k8s.io', 'roles', 'create', ''),
            ('rbac.authorization.k8s.io', 'rolebindings', 'create', ''),
            ('rbac.authorization.k8s.io', 'clusterroles', 'create', ''),
            ('', 'configmaps', 'update', 'unrelated'), ('batch', 'cronjobs', 'patch', 'railshot-credentials')):
            self.assertFalse(allowed(group, resource, verb, name))
        secret = 'railshot-' + self.target
        self.assertFalse(allowed('', 'secrets', 'patch', secret))
        self.assertEqual(self.run_registration()['status'], 'succeeded')
        self.assertTrue(allowed('', 'secrets', 'patch', secret))
        self.assertFalse(allowed('', 'secrets', 'get', 'unrelated'))
        self.assertFalse(allowed('', 'secrets', 'get', secret, 'kube-system'))

    def test_foreign_namespace_is_not_adopted(self):
        self.runtime.objects['default', 'namespace', 'app-new'] = {'metadata': {'name': 'app-new', 'labels': {'owner': 'someone-else'}}}
        result = self.run_registration()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.runtime.applications, 0)
        self.assertIsNone(self.variable)

    def test_broad_bootstrap_role_blocks_before_resource_creation(self):
        self.control.objects['argocd', 'role', 'railshot-product-registrations']['rules'] = [
            {'apiGroups': [''], 'resources': ['secrets'], 'verbs': ['get', 'patch'], 'resourceNames': []}]
        result = self.run_registration()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['stage'], 'permissions')
        self.assertEqual(self.runtime.applications, 0)
        self.assertEqual(self.control.applications, 0)

    def test_existing_nodeport_is_not_overwritten(self):
        self.runtime.objects['old-ns', 'service', 'old-app'] = {'metadata': {'namespace': 'old-ns', 'name': 'old-app'},
            'spec': {'ports': [{'nodePort': 30085}]}}
        result = self.run_registration()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.runtime.applications, 0)

    def test_gcp_aws_edge_is_rejected_before_registration_side_effects(self):
        descriptor = json.loads((ROOT / 'examples/ansible/gcp-node-descriptor.json').read_text())
        descriptor['target_id'] = self.target
        self.write('descriptor.json', descriptor)
        self.config['registration'].update(edge_config_file='/private/edge.json', expires_at='2099-01-01T00:00:00Z')
        self.write('config.json', self.config)
        with self.assertRaisesRegex(Exception, 'AWS edge is AWS-only'):
            self.run_registration()
        self.assertFalse(self.shared.exists())
        self.assertFalse(self.home.exists())
        self.assertEqual(self.runtime.applications, 0)
        self.assertEqual(self.control.applications, 0)
        self.assertIsNone(self.variable)

    def test_missing_edge_expiry_blocks_before_registration_claim_or_remote_calls(self):
        self.config['registration'].update(edge_config_file='/private/edge.json', expires_at=None)
        self.write('config.json', self.config)
        with self.assertRaisesRegex(Exception, 'future UTC edge expiry'):
            self.run_registration()
        self.assertFalse(self.shared.exists())
        self.assertEqual(self.runtime.applications, 0)

    def test_observability_hook_receives_verified_identity_without_claiming_collection(self):
        self.config['registration']['observability_config_file'] = '/private/observer.json'
        self.write('config.json', self.config)
        def execute(argv, timeout, child_env):
            request = env.read_private(argv[argv.index('--request') + 1])
            self.assertEqual(request['node_ip'], self.descriptor['addresses']['private'])
            self.assertEqual(request['target_id'], self.target)
            self.assertEqual(request['registry_file'], str(self.root / 'registry.json'))
            self.assertEqual(timeout, 900)
            env.save(Path(argv[argv.index('--out') + 1]), {'status': 'succeeded', 'target_id': self.target,
                'app': 'new-app', 'registered': True, 'collection_state': 'pending'})
            return 0
        with patch.object(env.ansible, 'execute', side_effect=execute):
            result = self.run_registration()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['observability'], {'registered': True, 'collection_state': 'pending'})
        self.assertLess(result['steps'].index('observability'), result['steps'].index('ci'))

    def test_db_address_derived_only_from_private_binding(self):
        target = self.config['cd']['targets'][self.target]['target']
        target['database'] = {'runtime_secret': 'runtime-db', 'migration_secret': 'migration-db', 'ca_secret': 'database-ca'}
        ca = self.write('db-ca', '-----BEGIN CERTIFICATE-----\nfake', raw=True)
        binding = {'version': 1, 'host': '10.1.1.2', 'port': 5432, 'database': 'app', 'sslmode': 'verify-full', 'sslrootcert': str(ca),
            'runtime': {'username': 'app', 'password': 'synthetic-password'}, 'migration': {'username': 'migrate', 'password': 'synthetic-password2'}}
        self.write('binding.json', binding); self.write('config.json', self.config)
        _, cd, *_ = env.load(self.root / 'registry.json', self.target, self.root / 'config.json', self.root / 'binding.json')
        self.assertEqual(cd['targets'][self.target]['target']['database']['host'], binding['host'])
        target['database']['host'] = '10.0.0.99'; self.write('config.json', self.config)
        with self.assertRaisesRegex(Exception, 'database TLS binding differs'):
            env.load(self.root / 'registry.json', self.target, self.root / 'config.json', self.root / 'binding.json')

    def test_aws_group_is_attached_to_descriptor_instance(self):
        response = {'Reservations': [{'Instances': [{'InstanceId': 'i-0123456789abcdef0',
            'PrivateIpAddress': self.descriptor['addresses']['private'], 'SecurityGroups': [{'GroupId': 'sg-12345678'}]}]}]}
        with patch.object(env.argo, 'native', return_value=json.dumps(response)):
            self.assertEqual(env.aws_security_group(self.descriptor), 'sg-12345678')
            with self.assertRaisesRegex(Exception, 'attached runtime security group'):
                env.aws_security_group(self.descriptor, 'sg-99999999')
        response['Reservations'][0]['Instances'][0]['SecurityGroups'].append({'GroupId': 'sg-87654321'})
        with patch.object(env.argo, 'native', return_value=json.dumps(response)):
            self.assertEqual(env.aws_security_group({**self.descriptor, 'security_group_id': 'sg-12345678'}), 'sg-12345678')
            with self.assertRaisesRegex(Exception, 'attached runtime security group'):
                env.aws_security_group(self.descriptor)

    def test_gcp_contract_and_db_secrets_remain_separate_and_private(self):
        descriptor = json.loads((ROOT / 'examples/ansible/gcp-node-descriptor.json').read_text()); descriptor['target_id'] = self.target
        self.write('descriptor.json', descriptor)
        request, cd, settings, pull, _, _ = env.load(self.root / 'registry.json', self.target, self.root / 'config.json')
        self.assertEqual(request['target']['provider'], 'gcp')
        target = cd['targets'][self.target]['target']
        target['database'] = {'host': '10.1.1.2', 'port': 5432, 'runtime_secret': 'runtime-db', 'migration_secret': 'migration-db', 'ca_secret': 'database-ca'}
        binding = {'host': '10.1.1.2', 'port': 5432, 'database': 'app', 'runtime': {'username': 'app', 'password': 'a:@/?'},
            'migration': {'username': 'migrate', 'password': 'other'}, 'ca': 'CA'}
        docs = env.runtime_documents(target, 'owner', pull, binding)
        secrets = {d['metadata']['name']: d for d in docs if d['kind'] == 'Secret'}
        url = base64.b64decode(secrets['runtime-db']['data']['DATABASE_URL']).decode()
        self.assertIn('a%3A%40%2F%3F', url)
        self.assertIn('sslmode=verify-full', url)
        self.assertEqual(set(secrets['migration-db']['data']), {'MIGRATION_DATABASE_URL'})
        rules = next(d['rules'] for d in docs if d['kind'] == 'Role')
        self.assertFalse(any('secrets' in r['resources'] or '*' in r['resources'] for r in rules))
        self.assertTrue(any('jobs' in r['resources'] for r in rules))
        self.assertEqual([r for r in rules if 'pods/log' in r['resources']],
                         [{'apiGroups': [''], 'resources': ['pods/log'], 'verbs': ['get']}])


class DirectSshTest(unittest.TestCase):
    def test_runtime_kubectl_uses_private_address_and_strict_host_verification(self):
        request = env.ansible.from_openstack({'id': 'server-1', 'project_id': 'project-1', 'status': 'ACTIVE',
            'addresses': [{'network': 'management', 'address': '10.26.1.5', 'version': 4}]},
            target_id='onprem-node', request_id='registration.onprem-node', operation='guest.check',
            resource_id='server-1', project_id='project-1', management_network='management', placement='onprem-a',
            architecture='amd64', initialization='cloud-init',
            ssh={'user': 'ubuntu', 'identity_file': '/private/key', 'known_hosts_file': '/private/hosts'})
        with patch.object(env.argo, 'native', return_value='{"items": []}') as native:
            with env.runtime_kubectl(request) as kube:
                self.assertEqual(kube('default', 'get', 'nodes', '-o', 'json'), {'items': []})
        command = native.call_args.args[0]
        self.assertIn('ubuntu@10.26.1.5', command)
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('ProxyCommand=none', command)
        self.assertIn('UserKnownHostsFile=/private/hosts', command)
        self.assertIn('sudo -n k3s kubectl', command[-1])

    def test_runtime_relay_keeps_the_original_vm_host_key_identity(self):
        request = env.ansible.from_openstack({'id': 'server-1', 'project_id': 'project-1', 'status': 'ACTIVE',
            'addresses': [{'network': 'management', 'address': '10.26.1.5', 'version': 4}]},
            target_id='onprem-node', request_id='registration.onprem-node', operation='guest.check',
            resource_id='server-1', project_id='project-1', management_network='management', placement='onprem-a',
            architecture='amd64', initialization='cloud-init', ssh={'user': 'ubuntu', 'identity_file': '/private/key',
                'known_hosts_file': '/private/hosts', 'connect_host': '172.31.0.172', 'port': 10022})
        with patch.object(env.argo, 'native', return_value='{"items": []}') as native:
            with env.runtime_kubectl(request) as kube:
                kube('default', 'get', 'nodes', '-o', 'json')
        command = native.call_args.args[0]
        self.assertIn('ubuntu@172.31.0.172', command)
        self.assertIn('HostKeyAlias=10.26.1.5', command)
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertEqual(command[command.index('-p') + 1], '10022')


class ObservationRegistryTest(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('observation_register', ROOT / 'observability/register.py')
        self.observation = importlib.util.module_from_spec(spec)
        # Full discovery has already imported deployment/scripts/render.py.
        foreign_renderer = object()
        with patch.dict(sys.modules, {'render': foreign_renderer}):
            spec.loader.exec_module(self.observation)
            self.assertIs(sys.modules['render'], foreign_renderer)
        self.config = {'owner': 'shared-observer', 'prometheus_url': 'http://10.0.0.20:9090',
            'observer_ip': '10.0.0.20', 'node_metrics_port': 31490, 'cluster_metrics_port': 31491}
        self.request = {'version': 1, 'target_id': 'new-openstack', 'environment_id': 'env-1', 'app': 'demo-app',
            'namespace': 'tenant-demo', 'node_ip': '10.26.1.6', 'probe_url': 'https://app.example.com/health',
            'registry_file': '/private/targets.json', 'context': 'control'}

    def test_registered_openstack_binding_is_used_for_observation_and_direct_ssh(self):
        import environment
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            def private(name, value):
                path = root / name
                path.write_text(json.dumps(value) if isinstance(value, dict) else value)
                path.chmod(0o600)
                return str(path)
            server = {'id': 'server-1', 'project_id': 'project-1', 'status': 'ACTIVE',
                'addresses': [{'network': 'management', 'address': '10.26.1.5', 'version': 4}]}
            target = {'server_file': private('server.json', server), 'resource_id': 'server-1', 'project_id': 'project-1',
                'management_network': 'management', 'placement': 'onprem-a', 'architecture': 'amd64',
                'initialization': 'cloud-init', 'purpose': 'runtime',
                'ssh': {'user': 'ubuntu', 'identity_file': private('key', 'synthetic-key'),
                        'known_hosts_file': private('hosts', 'synthetic-host'), 'connect_host': '172.31.0.172', 'port': 10022}}
            registry = private('registry.json', {'version': 1, 'targets': {'new-openstack': target}})
            request, resource = self.observation.node_request(registry, 'new-openstack')
            self.assertEqual(request['target']['provider'], 'openstack')
            self.assertEqual(request['timeout_seconds'], 300)
            self.assertNotIn('execution_driver', resource)
            row, _ = self.observation.registration_row(self.config, {**self.request, 'target_id': 'new-openstack', 'node_ip': '10.26.1.5'}, resource)
            self.assertEqual((row['resource_id'], row['node_ip'], row['node_instance']), ('server-1', '10.26.1.5', '172.31.0.172:31490'))
            with self.observation.observer_ssh({**self.config, 'observer_ip': '10.26.1.5'}, request, environment.ansible) as command:
                self.assertIn('ubuntu@172.31.0.172', command)
                self.assertIn('HostKeyAlias=10.26.1.5', command)
                self.assertIn('StrictHostKeyChecking=yes', command)
            with self.assertRaises(ValueError):
                self.observation.registration_row(self.config, {**self.request, 'target_id': 'new-openstack'}, resource)
            private('server.json', {**server, 'project_id': 'other'})
            with self.assertRaises(ValueError):
                self.observation.node_request(registry, 'new-openstack')

    def test_observation_keeps_existing_aws_and_gcp_descriptor_adapters(self):
        import environment
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            def private(name, value):
                path = root / name
                path.write_text(json.dumps(value) if isinstance(value, dict) else value); path.chmod(0o600)
                return str(path)
            for provider in ('aws', 'gcp'):
                with self.subTest(provider=provider):
                    descriptor = json.loads((environment.ROOT / f'examples/ansible/{provider}-node-descriptor.json').read_text())
                    descriptor['addresses']['metrics'] = '10.99.0.1'
                    target = {'descriptor_file': private('descriptor.json', descriptor),
                        'ssh': {'user': 'ubuntu', 'identity_file': private('key', 'synthetic-key'),
                                'known_hosts_file': private('hosts', 'synthetic-host')}}
                    registry = private('registry.json', {'version': 1, 'targets': {descriptor['target_id']: target}})
                    request, resource = self.observation.node_request(registry, descriptor['target_id'])
                    self.assertEqual(request['target']['provider'], provider)
                    expected = copy.deepcopy(descriptor)
                    expected['addresses'].pop('metrics', None)
                    self.assertEqual(resource, expected)
                    row, _ = self.observation.registration_row(self.config,
                        {**self.request, 'target_id': descriptor['target_id'], 'node_ip': descriptor['addresses']['private']}, resource)
                    self.assertEqual(row['node_instance'], descriptor['addresses']['private'] + ':31490')
                    self.assertEqual(self.observation.node_request(registry, descriptor['target_id'],
                        environment.read_private, environment.ansible), (request, resource))


if __name__ == '__main__':
    unittest.main()
