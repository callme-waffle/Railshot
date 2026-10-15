"""Explicit renewable ADC authority; no ambient gcloud user account or executable source."""
import os
from .native import GcpError, private_json, directory, command

from infrastructure.ansible.cloud_credentials import CredentialError, PROJECT, gcp_credential_identity


def credential_identity(path, project, expected_service_account=None):
    """Retain provider private-file handling and public error codes."""
    try:
        return gcp_credential_identity(private_json(path), path, project, expected_service_account)
    except CredentialError as error:
        raise GcpError(str(error)) from error


class Session:
    def __init__(self, config, project_id, state_dir, *, runner=command):
        if not isinstance(config, dict) or set(config) != {'mode', 'credentials_file', 'service_account'} or config['mode'] not in ('workload_identity', 'managed_service_account'):
            raise GcpError('GCP_AUTH_CONFIGURATION_INVALID')
        self.config, self.project, self.root, self.runner = config, project_id, directory(state_dir), runner
        self.identity = credential_identity(config['credentials_file'], project_id, config['service_account'])
        if config['mode'] == 'workload_identity' and self.identity['type'] != 'external_account': raise GcpError('GCP_WIF_REQUIRED')
    def env(self):
        current = credential_identity(self.config['credentials_file'], self.project, self.config['service_account'])
        if current != self.identity: raise GcpError('GCP_CREDENTIAL_AUTHORITY_CHANGED')
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'TF_PLUGIN_CACHE_DIR') if key in os.environ}
        env.update(GOOGLE_APPLICATION_CREDENTIALS=self.config['credentials_file'], CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=self.config['credentials_file'], CLOUDSDK_CORE_PROJECT=self.project, CLOUDSDK_CORE_DISABLE_PROMPTS='1', CLOUDSDK_CONFIG=str(self.root), TF_IN_AUTOMATION='1', TF_INPUT='0')
        return env
    def call(self, *args, mutation=False):
        import json
        return json.loads(self.runner(['gcloud', *args, '--project', self.project, '--format=json', '--quiet'], env=self.env(), mutation=mutation))
