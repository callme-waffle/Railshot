"""Security boundary tests for the server-assisted customer credential vault."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from apps.agent import openstack_control, personal
from deployment.bootstrap.client_setup.credentials import AssistedCredentialStore
from infrastructure.providers.openstack.cli import OpenStackCLI


AUTH = {'auth_url': 'https://keystone.example/v3', 'application_credential_id': 'app-id',
        'application_credential_secret': 'secret-on-client-only'}
BINDING = {'target_id': 'target1', 'generation': 1, 'project_id': 'project1'}


class VaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cls.public_pem = cls.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        cls.key_id = hashlib.sha256(cls.private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.vault = AssistedCredentialStore(self.home)
        self.vault.save(AUTH, **BINDING, public_key_pem=self.public_pem, key_id=self.key_id)
        wrapped = base64.b64decode(self.vault.document()['wrapped_dek'])
        self.dek = self.private.decrypt(wrapped, padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        self.unlock_key = base64.b64encode(self.dek).decode()

    def test_wrong_key_and_binding_cannot_unlock(self):
        self.assertEqual(self.vault.unlock(self.unlock_key, **BINDING), AUTH)
        with self.assertRaises(Exception):
            self.vault.unlock(base64.b64encode(b'X' * 32).decode(), **BINDING)
        with self.assertRaises(ValueError):
            self.vault.unlock(self.unlock_key, target_id='other', generation=1, project_id='project1')
        with self.assertRaises(ValueError):
            self.vault.unlock(self.unlock_key, target_id='target1', generation=2, project_id='project1')
        self.assertFalse((self.home / 'credentials.key').exists())
        self.assertNotIn(AUTH['application_credential_secret'], (self.home / 'credentials.vault').read_text())

    def test_revocation_owner_secret_uses_same_server_assisted_key(self):
        owner = AssistedCredentialStore(self.home / 'identity-owner')
        owner_auth = {'auth_url': AUTH['auth_url'], 'project_id': 'project1',
                      'user_id': 'service-user', 'password': 'owner-password'}
        owner.save(owner_auth, **BINDING, public_key_pem=self.public_pem,
                   key_id=self.key_id, data_key=self.dek)
        self.assertEqual(owner.unlock(self.unlock_key, **BINDING), owner_auth)
        self.assertFalse((self.home / 'identity-owner' / 'credentials.key').exists())
        self.assertNotIn('owner-password', owner.path.read_text())

    def test_replay_and_real_cli_adapter_shape(self):
        class FakeCloud:
            def __init__(self):
                self.calls = []

            def run(self, argv):
                self.calls.append(argv)
                if argv == ['token', 'issue']:
                    return {'id': 'scoped-token', 'project_id': 'project1'}
                if argv == ['server', 'list']:
                    return [{'ID': 'server1', 'Name': 'web', 'Status': 'ACTIVE'}]
                raise AssertionError(argv)

        cloud = FakeCloud()
        request = {'version': 1, 'job_id': 'job1', 'action': 'openstack.execute',
                   'target_id': 'target1', 'generation': 1, 'unlock_key': self.unlock_key,
                   'params': {'argv': ['server', 'list']}}
        call = lambda value: openstack_control.execute_raw(json.dumps(value).encode(),
                   openstack_control.COMMAND, self.vault.unlock, BINDING,
                   cli_factory=lambda auth: cloud if auth == AUTH else None, home=self.home)
        first = call(request)
        self.assertTrue(first['ok'])
        self.assertEqual(cloud.calls, [['token', 'issue'], ['server', 'list']])
        self.assertFalse(call(request)['ok'])
        self.assertEqual(cloud.calls, [['token', 'issue'], ['server', 'list']])
        self.assertNotIn(self.unlock_key, (self.home / 'unlock-job-job1.json').read_text())
        wrong_target = {**request, 'job_id': 'job2', 'target_id': 'other'}
        self.assertFalse(call(wrong_target)['ok'])
        self.assertFalse((self.home / 'unlock-job-job2.json').exists())

    def test_openstack_cli_secret_file_is_only_on_tmpfs_forced_command(self):
        if not Path('/dev/shm').is_dir():
            self.skipTest('tmpfs unavailable')
        seen = []
        def runner(argv, **kwargs):
            path = Path(kwargs['env']['OS_CLIENT_CONFIG_FILE'])
            self.assertEqual(path.parents[1], Path('/dev/shm'))
            self.assertEqual(json.loads(path.read_text())['clouds']['railshot']['auth'], AUTH)
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            seen.append(path)
            return type('Result', (), {'returncode': 0,
                'stdout': json.dumps([{'ID': 'server1'}]), 'stderr': ''})()
        result = OpenStackCLI(AUTH, runner=runner, temp_parent='/dev/shm').run(['server', 'list'])
        self.assertEqual(result, [{'ID': 'server1'}])
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].exists())

    def test_interrupted_identity_requires_reconciliation(self):
        state = self.home / 'state'
        state.mkdir(mode=0o700)
        (state / 'identity-ownership.json').write_text(json.dumps({'project_id': 'project1',
            'resources': [{'type': 'application_credential', 'id': 'app-id'}]}))
        (state / 'identity-progress.json').write_text(json.dumps({'status': 'pending'}))
        (state / 'identity-ownership.json').chmod(0o600)
        (state / 'identity-progress.json').chmod(0o600)
        config = {**BINDING, 'vault_public_key': self.public_pem, 'vault_key_id': self.key_id}
        with patch.object(personal, 'CONFIG', self.home), patch.object(personal, 'STATE', state):
            with self.assertRaisesRegex(ValueError, 'IDENTITY_PRIOR_OUTCOME_UNKNOWN'):
                personal.finish_identity(config, object())


if __name__ == '__main__':
    unittest.main()
