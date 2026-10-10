import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci/scripts'))
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
import openstack_routes as routes


class OpenStackRoutesTest(unittest.TestCase):
    def test_environment_registration_transport_is_fixed_and_receipt_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            def private(name):
                path = root / name
                path.write_text('test-only'); path.chmod(0o600)
                return str(path)
            connection = {'host': '10.0.0.34', 'port': 22, 'user': 'railshot-operator',
                'identity_file': private('key'), 'known_hosts_file': private('known'), 'host_key_alias': '10.0.0.34'}
            config = {'version': 1, 'provider': 'openstack', 'base_domain': 'railshot.io',
                      'runtime_private_address': '10.0.0.18', 'controller': connection, 'proxy': connection}
            base = {'version': 1, 'project_id': '1' * 32, 'loadbalancer_id': str(uuid.UUID(int=1)),
                'listener_id': str(uuid.UUID(int=2)), 'member_subnet_id': str(uuid.UUID(int=3)),
                'base_domain': 'railshot.io', 'network': {'amphora_port_id': str(uuid.UUID(int=4)),
                    'amphora_server_id': str(uuid.UUID(int=5)), 'amphora_private_address': '10.0.0.40'}}
            runtime = {'project_id': '2' * 32, 'server_id': str(uuid.UUID(int=6)),
                'port_id': str(uuid.UUID(int=7)), 'security_group_id': str(uuid.UUID(int=8)), 'private_address': '10.0.0.18'}
            args = {'environment_id': 'personal-' + str(uuid.UUID(int=100)), 'generation': 1, 'base': base, 'runtime': runtime}
            binding = routes.worker.registration_binding(base, args['environment_id'], 1, runtime)
            def respond(command, **kwargs):
                self.assertEqual(command[-1], routes.COMMAND)
                self.assertNotIn(args['environment_id'], ' '.join(command))
                self.assertFalse(kwargs['shell'])
                payload = json.loads(kwargs['input'])
                self.assertEqual(payload['binding'], binding)
                statuses = {'register-runtime': 'registered', 'verify-runtime': 'verified', 'unregister-runtime': 'unregistered'}
                return subprocess.CompletedProcess(command, 0, json.dumps({'status': statuses[payload['operation']],
                    'https_verified': False, 'binding': binding}), '')
            with patch.object(routes.subprocess, 'run', side_effect=respond):
                self.assertEqual(routes.verify_runtime(config, **args), binding)
                self.assertEqual(routes.register_runtime(config, **args), binding)
                self.assertEqual(routes.unregister_runtime({**config, 'worker_binding': binding})['status'], 'unregistered')
            with patch.object(routes.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 600)):
                with self.assertRaises(routes.RouteError) as raised:
                    routes.register_runtime(config, **args)
                self.assertTrue(raised.exception.unknown)
                with self.assertRaises(routes.RouteError) as raised:
                    routes.verify_runtime(config, **args)
                self.assertFalse(raised.exception.unknown)
            with patch.object(routes.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0,
                json.dumps({'status': 'registered', 'https_verified': False, 'binding': {**binding, 'generation': 2}}), '')):
                with self.assertRaises(routes.RouteError):
                    routes.register_runtime(config, **args)

    def test_fixed_worker_private_transport_and_bound_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            def private(name, content):
                path = root / name
                path.write_text(content)
                path.chmod(0o600)
                return str(path)
            connection = {'host': '10.0.0.34', 'port': 22, 'user': 'railshot-operator',
                          'identity_file': private('key', 'test-only'),
                          'known_hosts_file': private('known', 'test-only'), 'host_key_alias': '10.0.0.34'}
            config = {'version': 1, 'provider': 'openstack', 'base_domain': 'railshot.io',
                      'runtime_private_address': '10.0.0.17', 'controller': connection,
                      'proxy': {**connection, 'host': '172.31.0.172', 'port': 10022, 'host_key_alias': '10.0.0.17'}}
            path = private('config.json', json.dumps(config))
            request = {'application_id': 'app-' + 'a' * 24, 'hostname': 'user-app.railshot.io',
                       'private_address': '10.0.0.17', 'node_port': 32123, 'health_path': '/health'}
            receipt = {**request, 'status': 'configured', 'https_verified': False,
                       'network_rule_id': 'abcdef01-1234-1234-1234-123456789abc',
                       'resources': {name: '12345678-1234-1234-1234-123456789abc'
                                     for name in ('pool', 'member', 'monitor', 'policy', 'rule')}}
            with patch.object(routes.subprocess, 'run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, json.dumps(receipt), '')
                self.assertEqual(routes.ensure(path, request), receipt)
                command = run.call_args.args[0]
                self.assertEqual(command[-1], routes.COMMAND)
                self.assertNotIn(request['hostname'], ' '.join(command))
                self.assertIn('StrictHostKeyChecking=yes', command)
                self.assertTrue(any(value.startswith('ProxyCommand=ssh ') for value in command))
                self.assertEqual(json.loads(run.call_args.kwargs['input']), request)
                self.assertFalse(run.call_args.kwargs['shell'])
                for change in ({'private_address': '10.0.0.18'}, {'node_port': True},
                               {'hostname': 'other.example.com'}, {'health_path': '/health\nignored'}):
                    with self.subTest(change=change), self.assertRaises(ValueError):
                        routes.ensure(path, {**request, **change})
                self.assertEqual(run.call_count, 1)
                for status in ('blocked', 'unknown'):
                    run.return_value = subprocess.CompletedProcess([], 1, json.dumps({
                        'status': status, 'https_verified': False, 'reason': 'private-worker-diagnostic'}), '')
                    with self.subTest(status=status), self.assertRaises(routes.RouteError) as raised:
                        routes.ensure(path, request)
                    # A blocked readback may follow a successful pool/SG create.
                    self.assertTrue(raised.exception.unknown)
                    self.assertNotIn('private-worker', str(raised.exception))
                for stdout in ('private-non-json-output', json.dumps({'status': 'blocked', 'reason': 'invalid'})):
                    run.return_value = subprocess.CompletedProcess([], 1, stdout, '')
                    with self.assertRaises(routes.RouteError) as raised:
                        routes.ensure(path, request)
                    self.assertTrue(raised.exception.unknown)
                run.return_value = subprocess.CompletedProcess([], 0, json.dumps(receipt), '')
                run.return_value.stdout = json.dumps({key: value for key, value in receipt.items()
                                                     if key != 'network_rule_id'})
                with self.assertRaises(routes.RouteError) as raised:
                    routes.ensure(path, request)
                self.assertTrue(raised.exception.unknown)
                run.return_value.stdout = json.dumps({**receipt, 'hostname': 'wrong.railshot.io'})
                with self.assertRaises(routes.RouteError) as raised:
                    routes.ensure(path, request)
                self.assertTrue(raised.exception.unknown)
                run.reset_mock()
                run.side_effect = subprocess.TimeoutExpired('ssh', 600)
                with self.assertRaisesRegex(ValueError, 'OUTCOME_UNKNOWN'):
                    routes.ensure(path, request)
                self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
