"""Local cluster admission, replay, secret boundary and TLS checks; no cloud calls."""
from contextlib import nullcontext
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import yaml

import application_database
import cluster
import database


class ClusterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.write('identity', b'offline SSH key')
        self.write('known_hosts', b'offline trusted host key')
        template = json.loads((cluster.ansible.ROOT / 'examples/ansible/aws-node-descriptor.json').read_text())
        targets = {}
        self.nodes = [{'target_id': 'db1', 'roles': ['database', 'dcs']},
                      {'target_id': 'db2', 'roles': ['database', 'dcs']},
                      {'target_id': 'db3', 'roles': ['dcs', 'proxy']}]
        for index, selection in enumerate(self.nodes, 1):
            name = selection['target_id']
            instance = 'i-0123456789abcdef' + str(index)
            descriptor = {**template, 'target_id': name, 'resource_id': instance,
                'purpose': 'database', 'data_disk': {'resource_id': 'vol-test' + str(index),
                'mount_path': '/var/lib/postgresql', 'preservation': 'retain'},
                'transport_ref': 'ssm:ap-northeast-2:' + instance, 'addresses': {'private': f'10.77.0.{index}'}}
            self.write(name + '.json', descriptor)
            targets[name] = {'purpose': 'database', 'timeout_seconds': 30, 'descriptor_file': str(self.root / (name + '.json')),
                'ssh': {'user': 'railshot-operator', 'identity_file': str(self.root / 'identity'),
                        'known_hosts_file': str(self.root / 'known_hosts')}}
        self.registry = {'version': 1, 'targets': targets}
        self.spec = {'version': 1, 'request_id': 'cluster-001', 'cluster_name': 'product-demo',
                     'app_name': 'my-app', 'nodes': self.nodes, 'client_cidrs': ['10.78.0.0/24'], 'timeout_seconds': 30}
        self.write('registry.json', self.registry)
        self.write('spec.json', self.spec)
        self.commands, self.app_calls = [], []

    def write(self, name, value):
        cluster.ansible.durable_write(self.root / name, value if isinstance(value, bytes) else cluster.encoded(value))

    def fake_command(self, argv, timeout=60):
        self.commands.append(argv)
        if 'encrypt' in argv:
            Path(argv[-1]).write_text('$ANSIBLE_VAULT;1.1;AES256\nfixture encrypted data\n')
        else:
            for flag in ('-out', '-keyout'):
                if flag in argv:
                    Path(argv[argv.index(flag) + 1]).write_text('offline certificate fixture')

    def fake_database(self, request, **_):
        self.prepared = request
        return {**database.result_for(request), 'status': 'succeeded', 'database_ready': True}

    def fake_application(self, request, binding_file):
        self.app_calls.append(request)
        return {**database.result_for(request), 'status': 'succeeded', 'database_ready': True}

    def run_cluster(self, application_runner=None):
        with patch.object(cluster, 'command', self.fake_command), \
                patch.object(cluster.shutil, 'which', return_value='/offline/executable'):
            return cluster.run(str(self.root / 'registry.json'), str(self.root / 'spec.json'), self.root / 'state',
                database_runner=self.fake_database, application_runner=application_runner or self.fake_application)

    def test_profile_tls_secret_binding_and_replay_preserve_every_generated_byte(self):
        result = self.run_cluster()
        binding = cluster.read_json(result['binding_file'])
        self.assertEqual(result['binding_sha256'], cluster.digest(Path(result['binding_file']).read_bytes()))
        self.assertEqual(binding['host'], '10.77.0.3')
        self.assertEqual(binding['sslmode'], 'verify-full')
        self.assertNotEqual(binding['runtime']['username'], binding['migration']['username'])
        self.assertNotEqual(binding['runtime']['password'], binding['migration']['password'])
        self.assertRegex(binding['database'], r'^my_app_[a-f0-9]{12}$')
        for role in ('runtime', 'migration'):
            self.assertNotIn(binding[role]['password'], json.dumps(result))
        destination = Path(result['binding_file']).parent
        self.assertIn('IP:10.77.0.3', (destination / 'db1-postgres.ext').read_text())
        self.assertIn('IP:10.77.0.3', (destination / 'db2-postgres.ext').read_text())
        before = {path.name: path.read_bytes() for path in destination.iterdir()}
        count = len(self.commands)
        self.assertEqual(self.run_cluster(), result)
        self.assertEqual(len(self.commands), count)
        self.assertEqual(len(self.app_calls), 1)
        self.assertEqual({path.name: path.read_bytes() for path in destination.iterdir()}, before)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in destination.iterdir()))
        self.assertIn('10.77.0.3/32', self.prepared['profile']['client_cidrs'])
        self.assertIs(self.prepared['profile']['require_postgres_mount'], True)

    def test_changed_spec_descriptor_key_or_generated_file_cannot_rotate_credentials(self):
        result = self.run_cluster()
        original = Path(result['binding_file']).read_bytes()
        for filename, changed in [('spec.json', {**self.spec, 'request_id': 'different-request'}),
                                  ('identity', b'a different SSH identity')]:
            previous = (self.root / filename).read_bytes()
            self.write(filename, changed)
            with self.assertRaisesRegex(ValueError, 'CLUSTER_INPUT_CONFLICT'):
                self.run_cluster()
            self.write(filename, previous)
            self.assertEqual(Path(result['binding_file']).read_bytes(), original)
        cluster.ansible.durable_write(Path(result['binding_file']), b'{"tampered":true}')
        with self.assertRaisesRegex(ValueError, 'CLUSTER_FILES_CHANGED'):
            self.run_cluster()

    def test_registry_permissions_purpose_physical_alias_and_topology_are_checked_before_generation(self):
        for change in ('permissions', 'purpose', 'alias', 'topology', 'public-network', 'disk'):
            self.write('registry.json', self.registry)
            self.write('spec.json', self.spec)
            descriptor = cluster.read_json(self.root / 'db2.json')
            if change == 'permissions':
                (self.root / 'registry.json').chmod(0o644)
            elif change == 'purpose':
                registry = copy.deepcopy(self.registry)
                registry['targets']['db1']['purpose'] = 'runtime'
                self.write('registry.json', registry)
            elif change == 'alias':
                first = cluster.read_json(self.root / 'db1.json')
                self.write('db2.json', {**descriptor, 'resource_id': first['resource_id'], 'transport_ref': first['transport_ref']})
            elif change == 'topology':
                spec = copy.deepcopy(self.spec)
                spec['nodes'][1]['roles'] = ['dcs']
                self.write('spec.json', spec)
            elif change == 'public-network':
                self.write('spec.json', {**self.spec, 'client_cidrs': ['0.0.0.0/0']})
            else:
                self.write('db2.json', {**descriptor, 'data_disk': {'mount_path': '/var/lib/rancher'}})
            with self.assertRaises((ValueError, OSError), msg=change):
                self.run_cluster()
            self.write('db2.json', descriptor)
            self.assertEqual(self.commands, [])

    def test_app_stage_uses_private_vars_strict_ssh_and_requires_nonce_bound_receipt(self):
        calls = []
        def runner(argv, timeout, env):
            calls.append(argv)
            inventory = cluster.read_json(argv[2])
            self.assertEqual(set(inventory['db_nodes']['hosts']), {'db1', 'db2'})
            for host in inventory['db_nodes']['hosts'].values():
                self.assertIn('StrictHostKeyChecking=yes', host['ansible_ssh_common_args'])
            values = cluster.read_json(argv[-1][1:])
            self.assertNotIn(values['railshot_binding']['runtime']['password'], ' '.join(argv))
            cluster.ansible.durable_write(Path(values['railshot_application_receipt_path']),
                                         cluster.encoded(values['railshot_application_receipt']))
            return 0
        def app(request, path):
            with patch.object(cluster.ansible, 'forwarded_port', return_value=nullcontext(50222)):
                return cluster.application(request, path, runner=runner)
        self.assertEqual(self.run_cluster(app)['status'], 'succeeded')
        self.assertEqual(len(calls), 1)
        broken = {**self.prepared, 'request_id': 'app-broken', 'binding_sha256': '0' * 64}
        self.assertEqual(cluster.application(broken, self.root / 'state/product-demo/binding.json')['status'], 'blocked')

    def test_app_failure_keeps_credentials_and_blocks_unsafe_replay(self):
        def failed(request, _):
            return cluster.ansible.fail(database.result_for(request), 'failed', 'APPLICATION_DATABASE_UNPROVEN', 'fixture', unknown=True)
        first = self.run_cluster(failed)
        self.assertEqual(first['status'], 'failed')
        binding = (self.root / 'state/product-demo/binding.json').read_bytes()
        self.assertEqual(self.run_cluster()['status'], 'failed')
        self.assertEqual((self.root / 'state/product-demo/binding.json').read_bytes(), binding)
        self.assertEqual(self.app_calls, [])


    @unittest.skipUnless(shutil.which('openssl') and shutil.which('ansible-vault'), 'Native TLS/Vault tools unavailable')
    def test_native_ca_chain_proxy_san_vault_and_permissions(self):
        spec, body, nodes, _ = cluster.load(str(self.root / 'registry.json'), str(self.root / 'spec.json'))
        work = cluster.private_dir(self.root / 'native')
        cluster.generate(work, work, spec, body, nodes)
        cert = work / 'db1-postgres.crt'
        response = subprocess.run(['openssl', 'x509', '-in', str(cert), '-text', '-noout'],
                                  check=True, capture_output=True, text=True)
        self.assertIn('IP Address:10.77.0.3', response.stdout)
        subprocess.run(['openssl', 'verify', '-CAfile', str(work / 'postgres-ca.crt'), str(cert)],
                       check=True, capture_output=True)
        values = database.decrypt_vault(work / 'vault.yml', work / 'vault-password', 20)
        self.assertEqual(set(values), database.SECRETS)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in work.iterdir()))

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Native Ansible unavailable')
    def test_native_mount_guard_rejects_missing_and_root_devices(self):
        task = yaml.safe_load((cluster.ansible.HERE / 'roles/preflight/tasks/check_hosts.yml').read_text())[1]
        root = {'mount': '/', 'device': '/dev/root'}
        for mounts, valid in [([root], False),
                ([root, {'mount': '/var/lib/postgresql', 'device': '/dev/root'}], False),
                ([root, {'mount': '/var/lib/postgresql', 'device': '/dev/nvme1n1'}], True)]:
            playbook = [{'hosts': 'localhost', 'gather_facts': False, 'vars': {'railshot_require_postgres_mount': True,
                'ansible_facts': {'mounts': mounts}}, 'tasks': [task]}]
            (self.root / 'mount.yml').write_text(yaml.safe_dump(playbook))
            (self.root / 'inventory.json').write_text(json.dumps({'db_nodes': {'hosts': {'localhost': {'ansible_connection': 'local'}}}}))
            response = subprocess.run(['ansible-playbook', '-i', str(self.root / 'inventory.json'), str(self.root / 'mount.yml')],
                env=cluster.ansible.child_env(), capture_output=True, timeout=20)
            self.assertEqual(response.returncode == 0, valid, response.stdout.decode())


class ApplicationRoles(unittest.TestCase):
    def test_owner_runtime_privileges_tls_and_unowned_role_rejection(self):
        binding = {'version': 1, 'host': '10.77.0.3', 'port': 5432, 'sslmode': 'verify-full',
            'database': 'app_123', 'migration': {'username': 'app_123_owner', 'password': 'migration-secret'},
            'runtime': {'username': 'app_123_runtime', 'password': 'runtime-secret'}}
        calls, queries = [], []
        class SQL:
            SQL = str
            Identifier = staticmethod(lambda value: '"' + value + '"')
        class Connection:
            autocommit = False
            transaction = False
            unowned = False
            def __init__(self, config): self.config, self.result = config, None
            def __enter__(self): self.transaction = True; return self
            def __exit__(self, *_): self.transaction = False
            def close(self): pass
            def cursor(self): return Cursor(self)
        class Cursor:
            def __init__(self, connection): self.connection = connection
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def fetchone(self): return self.connection.result
            def execute(self, query, parameters=None):
                queries.append((query, parameters))
                connection = self.connection
                connection.result = None
                if query.startswith('CREATE DATABASE'):
                    assert connection.autocommit and not connection.transaction, 'CREATE DATABASE cannot run inside a transaction'
                elif query == 'SHOW server_version_num': connection.result = ('160015',)
                elif query == 'SELECT pg_is_in_recovery()': connection.result = (False,)
                elif "shobj_description(oid, 'pg_authid')" in query and connection.unowned:
                    connection.result = ('another-deployment',)
                elif query.startswith('SELECT current_user'):
                    connection.result = (connection.config['user'], connection.config['dbname'], True)
                elif query.startswith('SELECT has_database_privilege'): connection.result = (False, False)
        def connect(**config):
            calls.append(config)
            return Connection(config)
        application_database.configure(binding, connect, SQL)
        remote = [call for call in calls if call['host'] == binding['host']]
        self.assertEqual({call['user'] for call in remote}, {binding['migration']['username'], binding['runtime']['username']})
        self.assertTrue(all(call['sslmode'] == 'verify-full' and call['sslrootcert'] == '/etc/patroni/tls/postgres-ca.crt' for call in remote))
        self.assertIn(('GRANT USAGE ON SCHEMA public TO "app_123_runtime"', None), queries)
        self.assertIn(('ALTER DEFAULT PRIVILEGES FOR ROLE "app_123_owner" IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "app_123_runtime"', None), queries)
        self.assertFalse(any('GRANT CREATE' in query and 'runtime' in query for query, _ in queries))
        self.assertFalse(any('migration-secret' in query or 'runtime-secret' in query for query, _ in queries))
        Connection.unowned = True
        queries.clear()
        with self.assertRaisesRegex(ValueError, 'not owned'):
            application_database.configure(binding, connect, SQL)
        self.assertFalse(any(query.startswith(('CREATE ', 'ALTER ', 'GRANT ')) for query, _ in queries))


if __name__ == '__main__':
    unittest.main()
