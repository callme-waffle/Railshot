"""Bootstrap contract checks with explicit fake transports, never live acceptance."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from infrastructure.providers.environments import runtime
from infrastructure.providers.environments.control import read_private, save
from infrastructure.providers.environments.contract import ProviderError

import applications
import environment
import cluster_edge


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); self.id = 'env-' + 'a' * 24
        self.config = {'registry_file': str(self.home / 'registry.json'), 'cd': {'version': 1}, 'environments': {self.id: {
            'provider': 'aws', 'tenant': 'test', 'source_repository': 'test/repo', 'pull_secret_file': '/private/pull.json',
            'target': {}, 'ingress': {'ci_credentials_file': '/private/github.json', 'observability_config_file': str(self.home / 'observer.json')}}}}
        save(self.home / 'registry.json', {'targets': {self.id: {}}})
        save(self.home / 'observer.json', {'state_dir': str(self.home / 'observer')})
        self.context = {'environment_id': self.id, 'owner_id': 'account:alice', 'account_id': 'aws-test',
                        'state_dir': str(self.home / 'state'), 'spec': {'provider': 'aws'}}
        self.native = {'target': {'provider': 'aws', 'id': self.id}, 'runtime': {'profile': 'kubeadm-1c2w'}}
        for name, value in [('load_config', self.config)]:
            mock = patch.object(applications, name, return_value=value); mock.start(); self.addCleanup(mock.stop)
        node = patch.object(environment, 'registered_node', return_value=(self.native, {})); node.start(); self.addCleanup(node.stop)
        edge = patch.object(cluster_edge, 'validate', return_value={'fixed_routes': [{'hostname': 'bootstrap.example.com', 'node_port': 30080, 'health_path': '/'}]})
        edge.start(); self.addCleanup(edge.stop)

    def test_missing_writer_or_observer_blocks_before_registration(self):
        for key in ('ci_credentials_file', 'observability_config_file'):
            with self.subTest(key=key), patch.object(environment, 'register') as register:
                original = self.config['environments'][self.id]['ingress'].pop(key)
                with self.assertRaises(ProviderError): runtime.bootstrap('/private/apps.json', self.context)
                register.assert_not_called()
                self.config['environments'][self.id]['ingress'][key] = original

    def test_binding_requires_actual_registration_and_shared_cluster_readback(self):
        calls = []
        def register(registry, identifier, request_file, directory):
            request = read_private(request_file)
            self.assertEqual(request['registration']['ci_credentials_file'], '/private/github.json')
            self.assertNotIn('token', request['registration'])
            save(Path(directory) / 'cd.json', request['cd'])
            save(Path(directory) / 'registration.json', {'status': 'succeeded'})
            calls.append(identifier)
            return {'status': 'succeeded', 'deployment_supported': True, 'observability': {'registered': True}}
        with patch.object(environment, 'register', side_effect=register), patch.object(environment, 'shared_cluster_snapshot') as observe:
            result = runtime.bootstrap('/private/apps.json', self.context)
            receipt = read_private(result['application_binding_receipt_path'])
            self.assertEqual(calls, [self.id]); observe.assert_called_once()
            self.assertEqual(receipt['owner_id'], 'account:alice')
            self.assertEqual(receipt['observer_product_file'], str(self.home / 'observer/product.json'))
            self.assertTrue(receipt['observability_registered'])

    def test_failed_registration_cannot_issue_a_binding_receipt(self):
        with patch.object(environment, 'register', return_value={'status': 'unknown'}):
            with self.assertRaisesRegex(ProviderError, 'OUTCOME_UNKNOWN'):
                runtime.bootstrap('/private/apps.json', self.context)
        self.assertFalse((self.home / 'state/bootstrap' / self.id / 'binding.json').exists())

    def test_credential_or_cluster_readback_failure_cannot_issue_receipt(self):
        def register(*args):
            home = Path(args[-1]); save(home / 'cd.json', {'targets': {self.id: {}}})
            return {'status': 'succeeded', 'deployment_supported': True}
        with patch.object(environment, 'register', side_effect=register), patch.object(environment, 'shared_cluster_snapshot', side_effect=ValueError('readback failed')):
            with self.assertRaisesRegex(ValueError, 'readback failed'):
                runtime.bootstrap('/private/apps.json', self.context)
        self.assertFalse((self.home / 'state/bootstrap' / self.id / 'binding.json').exists())



class TeardownRecoveryTests(unittest.TestCase):
    setUp = BootstrapTests.setUp
    def test_interrupted_control_cleanup_resumes_saved_identity_without_registration_read(self):
        from contextlib import ExitStack, contextmanager
        import application_lifecycle as lifecycle
        import cluster_applications
        self.config['state_dir'] = str(self.home / 'apps')
        home = runtime.private_dir(self.home / 'state/bootstrap' / self.id)
        bound = {'environment_id': self.id, 'owner_id': self.context['owner_id'], 'account_id': self.context['account_id'],
                 'application_config_hash': runtime.digest(self.config)}
        save(home / 'binding.json', bound)
        save(home / 'cd.json', {'targets': {self.id: {'app': 'environment-bootstrap'}}})
        save(home / 'retained-volumes.json', {'volumes': []})
        expected = {'renewal': {'secret': 'owned-secret'}, 'shared': None, 'application_name': 'bootstrap'}
        save(home / 'teardown.json', {'status': 'unknown', 'environment_id': self.id, 'binding_hash': runtime.digest(bound),
             'expected': expected, 'secret': {'uid': 'owned-secret-uid'}, 'project': {'uid': 'owned-project-uid', 'spec_sha256': 'bound'},
             'namespace_uid': 'owned-ns-uid', 'completed': ['ci', 'renewal', 'grants', 'secret']})
        @contextmanager
        def kube(_):
            yield lambda *args, **kwargs: None
        with ExitStack() as stack:
            snapshot = stack.enter_context(patch.object(environment, 'shared_cluster_snapshot', side_effect=AssertionError('removed credential cannot be reloaded')))
            stack.enter_context(patch.object(environment, 'runtime_kubectl', kube))
            stack.enter_context(patch.object(lifecycle, 'control_for', return_value=lambda *args: None))
            deleted = stack.enter_context(patch.object(lifecycle, 'delete_control'))
            stack.enter_context(patch.object(lifecycle, 'revoke_control_grants'))
            stack.enter_context(patch.object(cluster_applications, 'teardown', side_effect=ValueError('edge unavailable')))
            with self.assertRaisesRegex(ValueError, 'edge unavailable'):
                runtime.teardown('/private/apps.json', self.context)
            deleted.assert_called_once()
            self.assertEqual(deleted.call_args.args[-1]['uid'], 'owned-project-uid')
            snapshot.assert_not_called()
        with patch.object(environment, 'runtime_kubectl', kube), patch.object(lifecycle, 'control_for'), \
             patch.object(lifecycle, 'delete_control', side_effect=AssertionError('completed step replayed')), \
             patch.object(cluster_applications, 'teardown', return_value={'retained_resources': []}):
            result = runtime.teardown('/private/apps.json', self.context)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['data_policy'], 'retain')

if __name__ == '__main__': unittest.main()
