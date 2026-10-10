"""Real registrar functions against synthetic Kubernetes APIs; no cloud or application deployment."""
import copy
import json
import importlib.util
import shlex
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import personal_runtime as personal
import environment as env
import test_environment as fixtures

_spec = importlib.util.spec_from_file_location('personal_registration_relay', Path(__file__).resolve().parents[3] / 'apps/agent/runtime_access.py')
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)
REAL_RUNTIME_KUBECTL = env.runtime_kubectl


class PersonalRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RegistrationTest(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.ident = 'personal-11111111-1111-4111-8111-111111111111'
        self.profile = {'provider': 'openstack', 'tenant': 'demo', 'source_repository': 'owner/source',
            'pull_secret_file': str(self.root / 'pull.json'), 'target': {'repo_url': 'https://github.com/owner/config',
                'architecture': 'amd64', 'ingress_cidrs': ['10.0.0.0/8'],
                'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '512Mi'}}},
            'ingress': {'base_domain': 'railshot.io'}}
        self.template = {'version': 1, 'state_dir': str(self.root / 'applications'), 'registry_file': str(self.root / 'registry.json'),
            'cd': {key: value for key, value in self.fixture.config['cd'].items() if key != 'targets'}, 'environments': {'template': self.profile}}
        self.fixture.write('template.json', self.template)
        self.fixture.write('edge-key', 'key', raw=True); self.fixture.write('edge-hosts', '10.0.0.10 ssh-ed25519 key', raw=True)
        connection = {'host': '10.0.0.10', 'port': 22, 'user': 'railshot', 'identity_file': str(self.root / 'edge-key'),
                      'known_hosts_file': str(self.root / 'edge-hosts'), 'host_key_alias': '10.0.0.10'}
        self.fixture.write('edge-template.json', {'version': 1, 'provider': 'openstack', 'base_domain': 'railshot.io',
                           'controller': connection, 'proxy': connection})
        self.fixture.write('worker-base.json', {'version': 1, 'project_id': '11111111111111111111111111111111',
            'loadbalancer_id': '11111111-1111-4111-8111-111111111111', 'listener_id': '22222222-2222-4222-8222-222222222222',
            'member_subnet_id': '33333333-3333-4333-8333-333333333333', 'base_domain': 'railshot.io',
            'network': {'amphora_port_id': '44444444-4444-4444-8444-444444444444',
                        'amphora_server_id': '55555555-5555-4555-8555-555555555555', 'amphora_private_address': '10.0.0.10'}})
        self.fixture.write('tunnel-credentials.json', {'AccountTag': 'operator', 'TunnelSecret': 'synthetic',
            'TunnelID': '66666666-6666-4666-8666-666666666666'})
        self.fixture.write('origin-ca.pem', '-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----\n', raw=True)
        self.fixture.write('tunnel-template.json', {'version': 1, 'base_domain': 'railshot.io', 'name': 'railshot-tunnel',
            'tunnel_id': '66666666-6666-4666-8666-666666666666', 'credentials_file': str(self.root / 'tunnel-credentials.json'),
            'origin_vip': '10.0.0.51', 'ca_file': str(self.root / 'origin-ca.pem')})
        self.fixture.write('dns-token', 'synthetic-dns-token', raw=True)
        self.fixture.write('dns.json', {'version': 1, 'zone_id': 'a' * 32, 'base_domain': 'railshot.io',
            'token_file': str(self.root / 'dns-token'), 'state_dir': str(self.root / 'dns-state')})
        self.config = self.fixture.write('personal.json', {'version': 1, 'application_template': str(self.root / 'template.json'),
            'template_environment': 'template', 'state_dir': str(self.root / 'personal'), 'route_profiles': {'default': {
                'management_network': 'private', 'placement': 'nova', 'edge_template': str(self.root / 'edge-template.json'),
                'worker_base_file': str(self.root / 'worker-base.json'), 'tunnel_template': str(self.root / 'tunnel-template.json'),
                'dns_config_file': str(self.root / 'dns.json')}}})
        self.request = {'action': 'prepare', 'target_id': self.ident, 'generation': 1, 'project_id': 'project-one', 'address': '10.80.0.2',
            'profile_id': 'default', 'expected_resource_id': None,
            'identity_file': str(self.root / 'key'), 'known_hosts_file': str(self.root / 'hosts'), 'evidence': {
                'resource_id': 'vm-one', 'private_ipv4': '10.0.0.2', 'management_network': 'private', 'placement': 'nova',
                'architecture': 'amd64', 'initialization': 'preconfigured', 'ssh_user': 'railshot-runtime', 'ssh_port': 2223,
                'provider_binding': {'port_id': 'port-one', 'security_group_id': 'sg-one'},
                'server': {'id': 'vm-one', 'project_id': 'project-one', 'status': 'ACTIVE',
                    'addresses': {'private': [{'addr': '10.0.0.2', 'version': 4}]}}}}
        self.fixture.write('template.json', self.template)
        self.worker_binding = {'environment_id': self.ident, 'generation': 1, 'base_sha256': 'a' * 64, 'binding_sha256': 'b' * 64}
        self.enterContext(patch.object(personal.openstack_routes, 'verify_runtime', return_value=self.worker_binding))
        self.enterContext(patch.object(personal.openstack_routes, 'register_runtime', return_value=self.worker_binding))
        self.enterContext(patch.object(personal.openstack_routes, 'unregister_runtime', return_value={
            'status': 'unregistered', 'https_verified': False, 'binding': self.worker_binding}))
        self.enterContext(patch.object(personal.subprocess, 'run', return_value=SimpleNamespace(stdout=b'ssh-ed25519 AAAAsynthetic')))
        self.enterContext(patch.object(env.credentials, 'customer', side_effect=lambda server, ca, token, path, doc, **kw:
            {'status': {'allowed': doc['spec']['resourceAttributes']['namespace'] == self.ident and doc['spec']['resourceAttributes']['resource'] == 'deployments'}}))
        original = fixtures.Kube.__call__
        def kubectl(kube, namespace, *args, document=None):
            if args[:2] == ('get', 'nodes'):
                return {'items': [{'status': {'addresses': [{'type': 'InternalIP', 'address': '10.0.0.2'}], 'conditions': [{'type': 'Ready', 'status': 'True'}]}}]}
            if args[:3] == ('get', 'namespace', 'kube-system'):
                return {'metadata': {'uid': 'real-fixture-cluster'}}
            if namespace == 'kube-system' and args[:3] == ('get', 'deployment', 'coredns'):
                return {'spec': {'replicas': 1}, 'status': {'readyReplicas': 1}}
            if args[:2] == ('delete', '--raw'):
                parts = args[2].split('/')
                kind = {'secrets': 'secret', 'configmaps': 'configmap', 'deployments': 'deployment', 'appprojects': 'appproject',
                        'serviceaccounts': 'serviceaccount', 'roles': 'role', 'rolebindings': 'rolebinding'}[parts[-2]]
                key = (namespace, kind, parts[-1])
                obj = kube.objects[key]
                self.assertEqual(document['preconditions'], {name: obj['metadata'][name] for name in ('uid', 'resourceVersion')})
                del kube.objects[key]
                return {}
            return original(kube, namespace, *args, document=document)
        self.enterContext(patch.object(fixtures.Kube, '__call__', kubectl))
        self.enterContext(patch.object(env, 'runtime_kubectl', REAL_RUNTIME_KUBECTL))
        def transport(argv, document=None):
            # Keep native SSH argv construction and the real customer parser. Only the VM API is synthetic.
            relay.parse(argv[-1], document, self.ident)
            words = shlex.split(argv[-1])
            value = self.fixture.runtime(words[6], *words[7:], document=document)
            return json.dumps(value) if value is not None else ''
        self.enterContext(patch.object(env.argo, 'native', transport))

    def run_helper(self, action='prepare'):
        return personal.execute(self.config, {**self.request, 'action': action})

    def test_new_random_environment_registers_real_policy_and_verifies_without_application(self):
        before = copy.deepcopy(json.loads(self.fixture.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json']))
        result = self.run_helper()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['application_config_path'], str(self.root / 'personal' / self.ident / 'applications.json'))
        generated = env.read_private(result['application_config_path'])
        ingress = generated['environments'][self.ident]['ingress']
        self.assertEqual(ingress['runtime_binding'], {'project_id': 'project-one', 'resource_id': 'vm-one', 'private_ipv4': '10.0.0.2'})
        edge = env.read_private(ingress['edge_config_file']); tunnel = env.read_private(ingress['tunnel_config_file'])
        self.assertEqual(edge['worker_binding'], self.worker_binding)
        self.assertEqual((edge['runtime_private_address'], tunnel['environment_id'], tunnel['registry_file']),
                         ('10.0.0.2', self.ident, generated['registry_file']))
        self.assertNotIn('credentials_file', tunnel)
        policy = json.loads(self.fixture.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json'])
        self.assertEqual(policy['targets'][:-1], before['targets'])
        self.assertEqual(policy['targets'][-1]['target_id'], self.ident)
        self.assertFalse(any(kind in ('application', 'deployment') for _, kind, _ in self.fixture.control.objects))
        workloads = [(namespace, kind, name) for namespace, kind, name in self.fixture.runtime.objects
                     if kind in ('deployment', 'job')]
        self.assertEqual(workloads, [(self.ident, 'deployment', 'railshot-tunnel')])
        changes = (self.fixture.control.applications, self.fixture.runtime.applications)
        self.assertEqual(self.run_helper('verify')['status'], 'succeeded')
        self.assertEqual(changes, (self.fixture.control.applications, self.fixture.runtime.applications))
        self.fixture.runtime.objects[self.ident, 'serviceaccount', env.SA]['metadata']['uid'] = 'replacement'
        self.assertEqual(self.run_helper('verify')['status'], 'blocked')

    def test_personal_binding_is_accepted_by_existing_application_registrar(self):
        result = self.run_helper()
        self.assertEqual(result['status'], 'succeeded', result)
        app_id = personal.apps.application_id(self.ident, 'demo', 'first-app')
        with patch.object(env.credentials, 'customer', side_effect=lambda server, ca, token, path, doc, **kw:
                {'status': {'allowed': (doc['spec']['resourceAttributes']['namespace'] == self.ident
                    or doc['spec']['resourceAttributes']['namespace'].startswith('app-'))
                    and doc['spec']['resourceAttributes']['resource'] == 'deployments'}}):
            application = personal.apps.register(result['application_config_path'],
                {'environment_id': self.ident, 'app': 'first-app', 'application_id': app_id})
            self.assertEqual(application['status'], 'succeeded', application)
            self.assertEqual(self.run_helper('verify')['status'], 'succeeded')
        workloads = [(namespace, kind, name) for namespace, kind, name in self.fixture.runtime.objects
                     if kind in ('deployment', 'job')]
        self.assertEqual(workloads, [(self.ident, 'deployment', 'railshot-tunnel')])
        self.assertEqual(self.run_helper('delete')['status'], 'unknown')
        self.assertIn((self.ident, 'serviceaccount', env.SA), self.fixture.runtime.objects)

    def test_delete_revokes_only_environment_objects_preserves_namespace_and_other_policy(self):
        self.assertEqual(self.run_helper()['status'], 'succeeded')
        result = self.run_helper('delete')
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertTrue(result['revocation_verified'])
        self.assertIn(('default', 'namespace', self.ident), self.fixture.runtime.objects)
        self.assertNotIn((self.ident, 'serviceaccount', env.SA), self.fixture.runtime.objects)
        self.assertNotIn(('argocd', 'secret', 'railshot-' + self.ident), self.fixture.control.objects)
        policy = json.loads(self.fixture.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json'])
        self.assertEqual([row['target_id'] for row in policy['targets']], ['old-target'])
        claim = env.read_private(self.root / 'applications' / 'route-authorities' /
            (personal.digest('66666666-6666-4666-8666-666666666666') + '.json'))
        self.assertEqual((claim['profile_id'], claim['tunnel_id'], claim['status']),
                         ('default', '66666666-6666-4666-8666-666666666666', 'released'))
        self.assertNotEqual(self.run_helper('prepare')['status'], 'succeeded')

    def test_reconcile_completed_registration_after_lost_receipt_performs_no_external_writes(self):
        self.assertEqual(self.run_helper()['status'], 'succeeded')
        path = self.root / 'personal' / self.ident / 'registration.json'
        saved = env.read_private(path)
        env.save(path, {'status': 'unknown', 'input_sha256': saved['input_sha256']})
        before = (self.fixture.control.applications, self.fixture.runtime.applications)
        with patch.object(personal.openstack_routes, 'register_runtime', side_effect=AssertionError('readback cannot mutate')):
            result = self.run_helper('reconcile')
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(before, (self.fixture.control.applications, self.fixture.runtime.applications))
        self.assertEqual(env.read_private(path)['binding'], saved['binding'])

    def test_reconcile_unstarted_registration_is_explicitly_resumable(self):
        result = self.run_helper('reconcile')
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(result['resumable'])
        self.assertFalse((self.root / 'personal' / self.ident / 'registration.json').exists())
        self.assertEqual(self.run_helper()['status'], 'succeeded')

    def test_partial_runtime_removal_resumes_only_original_remaining_object_ids(self):
        self.assertEqual(self.run_helper()['status'], 'succeeded')
        original = personal.delete_exact
        count = 0
        def interrupted(*args):
            nonlocal count
            original(*args); count += 1
            if count == 2:
                raise RuntimeError('lost delete reply')
        with patch.object(personal, 'delete_exact', side_effect=interrupted):
            self.assertEqual(self.run_helper('delete')['status'], 'unknown')
        self.assertEqual(env.read_private(self.root / 'personal' / self.ident / 'registration.json')['status'], 'succeeded')
        before = copy.deepcopy(self.fixture.runtime.objects)
        result = self.run_helper('inspect-delete')
        self.assertTrue(result['resumable'], result)
        self.assertEqual(before, self.fixture.runtime.objects)
        result = self.run_helper('resume-delete')
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertIn(('default', 'namespace', self.ident), self.fixture.runtime.objects)
        self.assertNotIn((self.ident, 'serviceaccount', env.SA), self.fixture.runtime.objects)

    def test_partial_removal_rejects_replaced_remaining_object(self):
        self.assertEqual(self.run_helper()['status'], 'succeeded')
        with patch.object(personal, 'remove_environment', side_effect=RuntimeError('interrupted')):
            self.assertEqual(self.run_helper('delete')['status'], 'unknown')
        self.fixture.runtime.objects[self.ident, 'serviceaccount', env.SA]['metadata']['uid'] = 'foreign-replacement'
        before = copy.deepcopy(self.fixture.runtime.objects)
        result = self.run_helper('inspect-delete')
        self.assertFalse(result.get('resumable', False))
        self.assertEqual(result['blockers'], [{'code': 'RUNTIME_DELETE_IDENTITY_CHANGED'}])
        self.assertEqual(before, self.fixture.runtime.objects)

    def test_automatic_profile_selection_uses_free_capacity_and_released_tombstones(self):
        _, _, _, profiles = personal.load_operator_config(self.config)
        spare = copy.deepcopy(profiles['default'])
        spare['tunnel']['tunnel_id'] = '77777777-7777-4777-8777-777777777777'
        profiles = {'default': profiles['default'], 'spare': spare}
        request = copy.deepcopy(self.request); request['profile_id'] = None
        claims = self.root / 'route-profile-pool'; claims.mkdir()
        self.assertEqual(personal.select_route_profile(profiles, request, claims)[0], 'default')
        default = claims / (personal.digest('66666666-6666-4666-8666-666666666666') + '.json')
        env.save(default, {'environment_id': 'personal-22222222-2222-4222-8222-222222222222',
                           'project_id': 'foreign', 'resource_id': 'vm-other', 'profile_id': 'default',
                           'tunnel_id': '66666666-6666-4666-8666-666666666666'})
        self.assertEqual(personal.select_route_profile(profiles, request, claims)[0], 'spare')
        env.save(default, {'environment_id': 'personal-22222222-2222-4222-8222-222222222222',
                           'project_id': 'foreign', 'resource_id': 'vm-other', 'profile_id': 'default',
                           'tunnel_id': '66666666-6666-4666-8666-666666666666', 'status': 'released'})
        self.assertEqual(personal.select_route_profile(profiles, request, claims)[0], 'default')

    def test_openstack_cli_address_strings_include_ipv6_and_ipv4(self):
        self.request['evidence']['server']['addresses'] = {'private': ['fd12::2', '10.0.0.2']}
        self.assertEqual(self.run_helper()['status'], 'succeeded')

    def test_ambiguous_management_ipv4_is_rejected(self):
        self.request['evidence']['server']['addresses'] = {'private': ['10.0.0.2', '10.0.0.3']}
        result = self.run_helper()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['blockers'], [{'code': 'RUNTIME_ADDRESS_MISMATCH'}])

    def test_scope_mismatch_blocks_before_any_external_write(self):
        self.request['evidence']['server']['project_id'] = 'foreign'
        result = self.run_helper()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.fixture.control.applications, 0)
        self.assertEqual(self.fixture.runtime.applications, 0)

    def test_other_environment_route_is_not_adopted(self):
        path = self.root / 'applications' / 'route-authorities' / (personal.digest('66666666-6666-4666-8666-666666666666') + '.json')
        personal.apps.private_directory(path.parent); personal.runtime.save(path, {'environment_id': 'other'})
        result = self.run_helper()
        self.assertEqual(result['blockers'], [{'code': 'ENVIRONMENT_OWNERSHIP_CONFLICT'}])
        self.assertEqual(self.fixture.control.applications, 0)

    def test_partial_registration_is_not_replayed(self):
        with patch.object(env, 'install_renewal', side_effect=RuntimeError('interrupted')):
            self.assertEqual(self.run_helper()['status'], 'unknown')
        changes = (self.fixture.control.applications, self.fixture.runtime.applications)
        self.assertEqual(self.run_helper()['status'], 'unknown')
        self.assertEqual(changes, (self.fixture.control.applications, self.fixture.runtime.applications))

    def test_static_readiness_rejects_missing_operator_secret_without_exposing_it(self):
        (self.root / 'dns-token').unlink()
        result = personal.check_config(self.config)
        self.assertFalse(result['ready'])
        self.assertEqual(result['blockers'], [{'code': 'RUNTIME_DNS_CONFIGURATION_INVALID'}])
        self.assertNotIn('synthetic', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
