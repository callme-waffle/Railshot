"""Stable credential identities without installing a provider implementation."""
import copy
from pathlib import Path
import tempfile
import unittest

from infrastructure.ansible.cloud_credentials import CredentialError, gcp_credential_identity


class CredentialIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = 'railshot-project'
        self.email = 'fixture@railshot-project.iam.gserviceaccount.com'
        self.source = self.root / 'subject'
        self.source.write_text('subject-one')
        self.source.chmod(0o600)
        self.adc = self.root / 'adc.json'
        self.data = {
            'type': 'external_account',
            'audience': '//iam.googleapis.com/projects/123456789012/locations/global/workloadIdentityPools/customers/providers/company',
            'subject_token_type': 'urn:ietf:params:oauth:token-type:jwt',
            'token_url': 'https://sts.googleapis.com/v1/token',
            'service_account_impersonation_url': 'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/' + self.email + ':generateAccessToken',
            'credential_source': {'file': str(self.source)},
        }

    def identity(self, data=None):
        return gcp_credential_identity(data or self.data, self.adc, self.project, self.email)

    def test_rotating_subject_preserves_authority_but_changed_audience_does_not(self):
        first = self.identity()
        self.source.write_text('subject-two')
        self.assertEqual(first, self.identity())
        self.data['audience'] += '-other'
        self.assertNotEqual(first, self.identity())
        self.assertNotIn('subject-two', str(first))

    def test_executable_url_foreign_identity_and_public_token_are_rejected(self):
        for source in ({'executable': {'command': 'untrusted'}}, {'url': 'https://example.com/token'}):
            with self.subTest(source=source):
                value = copy.deepcopy(self.data)
                value['credential_source'] = source
                with self.assertRaisesRegex(CredentialError, 'FILE_SOURCE_REQUIRED'):
                    self.identity(value)
        value = copy.deepcopy(self.data)
        value['service_account_impersonation_url'] = value['service_account_impersonation_url'].replace(self.project, 'another-project')
        with self.assertRaisesRegex(CredentialError, 'SCOPE_MISMATCH'):
            self.identity(value)
        self.source.chmod(0o644)
        with self.assertRaisesRegex(CredentialError, 'FILE_SOURCE_REQUIRED'):
            self.identity()

    def test_key_rotation_uses_service_account_identity(self):
        data = {'type': 'service_account', 'project_id': self.project, 'client_email': self.email,
                'private_key': '-----BEGIN PRIVATE KEY-----one', 'token_uri': 'https://oauth2.googleapis.com/token'}
        first = self.identity(data)
        data['private_key'] = '-----BEGIN PRIVATE KEY-----two'
        self.assertEqual(first, self.identity(data))
        data['client_email'] = 'another@railshot-project.iam.gserviceaccount.com'
        with self.assertRaisesRegex(CredentialError, 'SCOPE_MISMATCH'):
            self.identity(data)
