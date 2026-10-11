import importlib.util
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'deployment/bootstrap/runtime-healthz.py'
spec = importlib.util.spec_from_file_location('native_health', SOURCE)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class RuntimeHealthTests(unittest.TestCase):
    def test_only_managed_runtime_config_and_healthz_are_accepted(self):
        before = (health.MARKER + '\nnode-ip: "10.0.0.17"\n').encode()
        after = health.desired_config(before)
        self.assertEqual(health.desired_config(after), after)
        self.assertEqual(health.AUTH['anonymous']['conditions'], [{'path': '/healthz'}])
        for value in [b'node-ip: 10.0.0.17\n', before + b'kube-apiserver-arg:\n - anonymous-auth=true\n',
                      before + b'server: https://control:6443\n']:
            with self.subTest(config=value), self.assertRaises(ValueError):
                health.desired_config(value)

    def environment(self, root):
        (root / 'run').mkdir()
        config = root / 'etc/rancher/k3s/config.yaml'
        config.parent.mkdir(parents=True)
        before = (health.MARKER + '\nnode-ip: "10.0.0.17"\n').encode()
        config.write_bytes(before)
        ca = root / 'var/lib/rancher/k3s/server/tls/server-ca.crt'
        ca.parent.mkdir(parents=True)
        ca.write_text('PUBLIC CERTIFICATE\n')
        calls = []
        def native(args):
            calls.append(args)
            if args == ['k3s', '--version']:
                return 'k3s version v1.34.11+k3s1 (test)\n'
            return 'ok\n'
        return config, before, calls, native

    def test_idempotent_apply_and_verified_rollback(self):
        for fail in [False, True]:
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config, before, calls, native = self.environment(root)
                def path(value):
                    return root / str(value).lstrip('/')
                checks = {'/healthz': 200, '/version': 401, '/api/v1/secrets': 401}
                with patch.object(health, 'Path', side_effect=path), patch.object(health, 'native', side_effect=native), \
                     patch.object(health, 'workload_identity', return_value=[('Deployment', 'tenant', 'app', 'uid', 'digest')]), \
                     patch.object(health, 'check_health', side_effect=ValueError('not healthy') if fail else None, return_value=checks), \
                     patch.object(health.time, 'monotonic', side_effect=iter([0, 100, 100] if fail else [0, 0])):
                    if fail:
                        with self.assertRaises(ValueError):
                            health.configure('10.0.0.17')
                        self.assertEqual(config.read_bytes(), before)
                        self.assertFalse(path(health.AUTH_PATH).exists())
                        receipt = json.loads(path('/var/lib/railshot/runtime-healthz/receipt.json').read_text())
                        self.assertTrue(receipt['rollback_verified'])
                        self.assertEqual(calls.count(['systemctl', 'restart', 'k3s']), 2)
                    else:
                        self.assertTrue(health.configure('10.0.0.17')['changed'])
                        self.assertFalse(health.configure('10.0.0.17')['changed'])
                        self.assertEqual(calls.count(['systemctl', 'restart', 'k3s']), 1)

    def test_control_role_is_rejected_before_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, calls, native = self.environment(root)
            role = root / 'etc/railshot/node-role'
            role.parent.mkdir(parents=True)
            role.write_text('control\n')
            with patch.object(health, 'Path', side_effect=lambda value: root / str(value).lstrip('/')), \
                 patch.object(health, 'native', side_effect=native), self.assertRaises(ValueError):
                health.configure('10.0.0.17')
            self.assertEqual(calls, [])

    def test_active_deployment_refuses_before_any_runtime_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, before, calls, native = self.environment(root)
            lock = os.open(root / 'run/railshot-deployment.lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(health, 'Path', side_effect=lambda value: root / str(value).lstrip('/')), \
                     patch.object(health, 'native', side_effect=native), self.assertRaisesRegex(ValueError, 'deployment is active'):
                    health.configure('10.0.0.17')
                self.assertEqual(config.read_bytes(), before)
                self.assertEqual(calls, [])
            finally:
                os.close(lock)


if __name__ == '__main__':
    unittest.main()
