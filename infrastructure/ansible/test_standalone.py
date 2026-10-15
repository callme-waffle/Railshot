"""Local boundary tests: no guest or cloud mutation."""
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import standalone
import standalone_guest as guest


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        for name in ('ca', 'cert', 'key', 'ssh', 'known'):
            (self.home / name).write_text('private-test-content'); (self.home / name).chmod(0o600)
        (self.home / 'password').write_text('test-probe-password-32-characters'); (self.home / 'password').chmod(0o600)
        self.config = {'version': 1, 'client_cidrs': ['10.72.0.0/16'], 'data_device': '/dev/disk/by-id/data', 'tls': {'server_name': 'db.internal.example', 'ca_file': str(self.home / 'ca'), 'certificate_file': str(self.home / 'cert'), 'key_file': str(self.home / 'key')}, 'probe_password_file': str(self.home / 'password'), 'backup': {'type': 's3', 'bucket': 'approved-bucket', 'prefix': '/postgres', 'retention_full': 2, 'identity': 'instance', 'endpoint': 's3.ap-northeast-2.amazonaws.com', 'region': 'ap-northeast-2'}}
        self.descriptor = {'version': 1, 'provider': 'openstack', 'environment_id': 'env-test', 'account_id': 'tenant', 'region': 'region1', 'resource_id': 'vm-owned', 'private_ipv4': '10.72.1.5', 'ssh': {'user': 'ubuntu', 'identity_file': str(self.home / 'ssh'), 'known_hosts_file': str(self.home / 'known'), 'port': 22}}
        self.context = {'environment_id': 'env-test', 'owner_id': 'account:alice', 'account_id': 'tenant', 'credential_ref': 'cred', 'policy_revision': 'v1', 'state_dir': str(self.home / 'state'), 'operation_id': 'op-install'}
        self.calls = 0
        self.valid = True
        self.receipt = True
    def runner(self, argv, *_):
        self.calls += 1
        variables = standalone.read(argv[-1][1:])
        config = standalone.read(variables['railshot_standalone_input'])
        if self.receipt:
            standalone.save(Path(variables['railshot_standalone_receipt']), {'nonce': config['nonce'] if self.valid else 'wrong', 'environment_id': config['environment_id'], 'resource_id': config['resource_id'], 'ready': True, 'system_identifier': '1234567890123456789', 'action': config['operation']['action'], 'backup': {'backup_id': '20261015-120000F', 'system_identifier': '1234567890123456789', 'proof': 'a' * 64}})
        return 0
    def run_action(self, action='install', context=None):
        with patch.object(standalone.shutil, 'which', return_value='/usr/bin/ansible-playbook'), patch.object(standalone.ansible, 'forwarded_port', return_value=nullcontext(None)):
            return standalone.run(action, self.descriptor, self.config, context or self.context, runner=self.runner)
    def test_exit_zero_without_nonce_receipt_is_unknown_and_blocks_new_mutation(self):
        self.receipt = False
        self.assertEqual(self.run_action()['status'], 'unknown')
        self.assertFalse(self.run_action(context={**self.context, 'operation_id': 'another'})['ready'])
        self.assertEqual(self.calls, 1)
    def test_wrong_nonce_is_not_ready(self):
        self.valid = False
        self.assertEqual(self.run_action()['error']['code'], 'DATABASE_READINESS_UNPROVEN')
    def test_replay_of_verified_receipt_does_not_run_again(self):
        first = self.run_action(); second = self.run_action()
        self.assertTrue(first['ready']); self.assertEqual(first, second); self.assertEqual(self.calls, 1)
    def test_owner_scope_rejected_before_runner(self):
        self.descriptor['environment_id'] = 'other'
        self.assertEqual(self.run_action()['error']['code'], 'DATABASE_DESCRIPTOR_SCOPE_MISMATCH'); self.assertEqual(self.calls, 0)
    def test_restore_cannot_trust_public_flag_or_arbitrary_path(self):
        result = self.run_action({'action': 'restore', 'backup_id': '20261015-120000F'})
        self.assertEqual(result['error']['code'], 'DATABASE_RESTORE_APPLICATIONS_ACTIVE')
        with self.assertRaises(standalone.StandaloneError): standalone.action_spec({'action': 'restore', 'backup_id': '../../data'})
    def test_config_rejects_public_hba_and_shell_endpoint(self):
        self.config['client_cidrs'] = ['0.0.0.0/0']
        self.assertFalse(standalone.validate_config(self.config)['valid'])
        self.config['client_cidrs'] = ['10.72.0.0/16']; self.config['backup']['endpoint'] = 'example\nrepo1-storage-verify-tls=n'
        self.assertFalse(standalone.validate_config(self.config)['valid'])
    def test_rotation_preserves_authority_reference(self):
        self.assertTrue(self.run_action()['ready'])
        (self.home / 'password').write_text('a-new-valid-rotated-probe-password')
        self.assertEqual(self.run_action()['status'], 'succeeded')
    def test_ncp_public_transport_requires_exact_owned_public_address(self):
        self.descriptor.update(provider='ncp', public_ipv4='8.8.8.8')
        self.descriptor['ssh']['connect_host'] = '8.8.8.8'
        self.assertEqual(standalone.normalize(self.descriptor, self.config, self.context)['ssh']['connect_host'], '8.8.8.8')
        self.descriptor['ssh']['connect_host'] = '8.8.4.4'
        with self.assertRaises(standalone.StandaloneError): standalone.normalize(self.descriptor, self.config, self.context)

    def test_schema_skeleton_never_authorizes_restore(self):
        template = standalone.config_template(self.home)
        self.assertEqual(template['files']['postgres-restore-approval.json']['approval_id'], '')
        self.assertFalse(standalone.validate_config(template['config'])['valid'])


class GuestTests(unittest.TestCase):
    def test_readiness_requires_the_application_private_ip_tls_identity(self):
        from types import SimpleNamespace
        config = {'private_ipv4': '10.72.1.14', 'probe_password': 'private-fixture', 'tls': {'server_name': 'db.internal.example'}}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); (home / 'pg_hba.conf').write_text('approved')
            for missing_ip_san in (True, False):
                calls = []
                def connect(**kwargs):
                    calls.append(kwargs)
                    if missing_ip_san and kwargs['host'] == config['private_ipv4']:
                        raise ValueError('certificate IP SAN mismatch')
                    cursor = SimpleNamespace(execute=lambda query: None, fetchone=lambda: (True,))
                    return nullcontext(SimpleNamespace(cursor=lambda: nullcontext(cursor)))
                with self.subTest(missing_ip_san=missing_ip_san), patch.dict(sys.modules, {'psycopg2': SimpleNamespace(connect=connect)}), \
                     patch.object(guest, 'CONFIG', home), patch.object(guest, 'owned'), patch.object(guest, 'hba_config', return_value='approved'), \
                     patch.object(guest, 'sql', side_effect=['10.72.1.14|/etc/railshot-postgres/tls/server.crt|/etc/railshot-postgres/tls/server.key', '1234567890123456789', '160001|f|on|scram-sha-256']), \
                     patch.object(guest, 'backrest') as repository:
                    if missing_ip_san:
                        with self.assertRaisesRegex(ValueError, 'IP SAN mismatch'): guest.verify(config)
                        repository.assert_not_called()
                    else:
                        self.assertEqual(guest.verify(config), '1234567890123456789')
                        repository.assert_called_once_with('check')
                self.assertEqual([call['host'] for call in calls], ['db.internal.example', '10.72.1.14'])
                self.assertTrue(all(call['hostaddr'] == '10.72.1.14' and call['sslmode'] == 'verify-full' for call in calls))

    def test_foreign_mount_fails_before_package_install(self):
        with patch.object(guest, 'owned', side_effect=ValueError('foreign')), patch.object(guest, 'command') as command:
            with self.assertRaises(ValueError): guest.install({})
            command.assert_not_called()
    def test_new_backup_requires_completed_native_record(self):
        config = {'nonce': 'a' * 64, 'environment_id': 'env-test'}
        with patch.object(guest, 'verify', return_value='1234567890123456789'), patch.object(guest, 'sql'), patch.object(guest, 'backrest', side_effect=['[{"backup":[]}]', '', '[{"status":{"code":0},"backup":[]}]']):
            with self.assertRaisesRegex(ValueError, 'newly completed'): guest.backup(config)
    def test_restore_without_capacity_never_stops_the_service(self):
        config = {'restore': {'backup_id': '20261015-120000F', 'system_identifier': '1234567890123456789'}, 'operation': {'backup_id': '20261015-120000F'}, 'operation_id': 'op1', 'environment_id': 'env-test'}
        free = type('FS', (), {'f_bavail': 1, 'f_frsize': 4096})()
        with patch.object(guest, 'owned'), patch.object(guest, 'verify', return_value='1234567890123456789'), patch.object(guest, 'sql', return_value='1048576'), patch.object(guest, 'backrest', return_value='[{"status":{"code":0},"backup":[{"label":"20261015-120000F","info":{"size":1048576}}]}]'), patch.object(guest.os, 'statvfs', return_value=free), patch.object(guest, 'command') as native:
            with self.assertRaisesRegex(ValueError, 'insufficient restore space'): guest.restore(config)
            native.assert_not_called()

    def test_restore_verifies_sql_proof_after_service_start_and_preserves_old_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            mount = Path(temporary); data = mount / '16/main'; data.mkdir(parents=True); (data / 'old').write_text('old')
            config = {'restore': {'backup_id': '20261015-120000F', 'system_identifier': '1234567890123456789', 'proof': 'proof'}, 'operation': {'backup_id': '20261015-120000F'}, 'operation_id': 'op1', 'environment_id': 'env-test'}
            calls = []
            def command(argv, **kwargs):
                calls.append(argv)
                return 'inactive' if 'show' in argv else ''
            with patch.object(guest, 'MOUNT', mount), patch.object(guest, 'DATA', data), patch.object(guest, 'owned'), patch.object(guest, 'verify', return_value='1234567890123456789'), patch.object(guest, 'sql', side_effect=['1048576', '0', 'wrong-proof']), patch.object(guest, 'command', side_effect=command), patch.object(guest, 'backrest', side_effect=['[{"status":{"code":0},"backup":[{"label":"20261015-120000F","info":{"size":1048576}}]}]', '']), patch.object(guest.pwd, 'getpwnam', return_value=type('User', (), {'pw_uid': 0})()), patch.object(guest.grp, 'getgrnam', return_value=type('Group', (), {'gr_gid': 0})()), patch.object(guest.os, 'chown'):
                with self.assertRaisesRegex(ValueError, 'SQL proof mismatch'): guest.restore(config)
            self.assertEqual((mount / 'retained/op1/old').read_text(), 'old')
            self.assertIn(['systemctl', 'start', 'postgresql@16-main'], calls)


if __name__ == '__main__': unittest.main()
