import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import standalone_binding as db
import environment as runtime


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.ca = self.home / 'ca'; self.ca.write_text('-----BEGIN CERTIFICATE-----\ntest'); self.ca.chmod(0o600)
        runtime.save(self.home / 'node.json', {'private_ipv4': '10.72.1.5', 'resource_id': 'owned-vm'})
        runtime.save(self.home / 'config.json', {'tls': {'ca_file': str(self.ca)}})
        runtime.save(self.home / 'context.json', {'environment_id': 'env-test'})
        runtime.save(self.home / 'bundle.json', {'version': 1, 'descriptor_file': str(self.home / 'node.json'), 'config_file': str(self.home / 'config.json'), 'context_file': str(self.home / 'context.json')})
        self.profile = {'ingress': {'standalone_database': str(self.home / 'bundle.json')}}
    def test_roles_are_distinct_stable_and_secrets_reach_existing_runtime_renderer(self):
        first = db.prepare(self.profile, 'env-test', 'app-' + 'a' * 24, self.home)
        second = db.prepare(self.profile, 'env-test', 'app-' + 'a' * 24, self.home)
        self.assertEqual(first['binding'], second['binding'])
        binding = first['binding']
        self.assertEqual(len({binding['database'], binding['runtime']['username'], binding['migration']['username']}), 3)
        target = {'namespace': 'app-test', 'image_pull_secret': {'name': 'pull'}, 'database': first['target']}
        documents = runtime.runtime_documents(target, 'app-test', {'auths': {}}, binding)
        secrets = [item for item in documents if item['kind'] == 'Secret']
        self.assertEqual(len(secrets), 4)
        role = next(item for item in documents if item['kind'] == 'Role')
        self.assertTrue(any(item.get('resources') == ['jobs'] for item in role['rules']))
    def test_other_environment_rejected_before_binding(self):
        with self.assertRaises(ValueError): db.prepare(self.profile, 'other-env', 'app-' + 'a' * 24, self.home)
        self.assertFalse((self.home / 'database-binding.json').exists())
    def test_zero_exit_without_ready_does_not_register_database(self):
        prepared = db.prepare(self.profile, 'env-test', 'app-' + 'a' * 24, self.home)
        response = type('Result', (), {'returncode': 0, 'stdout': '{"status":"succeeded","ready":false}'})()
        with patch.object(db.subprocess, 'run', return_value=response):
            with self.assertRaises(ValueError): db.install(prepared)


if __name__ == '__main__': unittest.main()
