"""Provider-neutral credential authority validation; never hashes rotating secret bytes.

Callers retain ownership of private JSON loading and their public error types.
This module performs no token exchange or provider imports.
"""
import os
from pathlib import Path
import re
import stat


class CredentialError(ValueError):
    pass


PROJECT = r'[a-z][a-z0-9-]{4,28}[a-z0-9]'


def gcp_credential_identity(data, path, project, expected_service_account=None):
    if not re.fullmatch(PROJECT, project): raise CredentialError('GCP_PROJECT_INVALID')
    if data.get('type') == 'service_account':
        email = data.get('client_email', '')
        if data.get('project_id') != project or data.get('token_uri') != 'https://oauth2.googleapis.com/token' or '-----BEGIN PRIVATE KEY-----' not in data.get('private_key', ''):
            raise CredentialError('GCP_SERVICE_ACCOUNT_INVALID')
        identity = {'type': data['type'], 'service_account': email, 'credentials_file': str(path)}
    elif data.get('type') == 'external_account':
        allowed = {'type', 'audience', 'subject_token_type', 'token_url', 'service_account_impersonation_url', 'credential_source', 'service_account_impersonation', 'quota_project_id', 'universe_domain'}
        if set(data) - allowed or data.get('token_url') != 'https://sts.googleapis.com/v1/token' or data.get('subject_token_type') != 'urn:ietf:params:oauth:token-type:jwt' or data.get('universe_domain', 'googleapis.com') != 'googleapis.com' or data.get('quota_project_id', project) != project:
            raise CredentialError('GCP_WIF_CONFIGURATION_INVALID')
        if not re.fullmatch(r'//iam.googleapis.com/projects/[0-9]+/locations/global/workloadIdentityPools/[a-z0-9-]+/providers/[a-z0-9-]+', data.get('audience', '')):
            raise CredentialError('GCP_WIF_AUDIENCE_INVALID')
        url = data.get('service_account_impersonation_url', '')
        match = re.fullmatch(r'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/([^/:]+):generateAccessToken', url)
        if not match: raise CredentialError('GCP_IMPERSONATION_REQUIRED')
        email = match[1]
        source = data.get('credential_source', {})
        if set(source) - {'file', 'format'} or not isinstance(source.get('file'), str): raise CredentialError('GCP_WIF_FILE_SOURCE_REQUIRED')
        source_path = Path(source['file'])
        if not source_path.is_absolute() or source_path.resolve() != source_path: raise CredentialError('GCP_WIF_FILE_SOURCE_REQUIRED')
        info = source_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or not 0 < info.st_size < 1024 * 1024: raise CredentialError('GCP_WIF_FILE_SOURCE_REQUIRED')
        fmt = source.get('format', {'type': 'text'})
        if fmt not in ({'type': 'text'},) and not (isinstance(fmt, dict) and set(fmt) == {'type', 'subject_token_field_name'} and fmt['type'] == 'json' and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', fmt['subject_token_field_name'])):
            raise CredentialError('GCP_WIF_FILE_FORMAT_INVALID')
        imp = data.get('service_account_impersonation', {})
        if set(imp) - {'token_lifetime_seconds'} or type(imp.get('token_lifetime_seconds', 3600)) is not int or not 600 <= imp.get('token_lifetime_seconds', 3600) <= 3600:
            raise CredentialError('GCP_WIF_LIFETIME_INVALID')
        identity = {'type': data['type'], 'service_account': email, 'audience': data['audience'], 'source_file': str(source_path), 'source_format': fmt, 'credentials_file': str(path)}
    else: raise CredentialError('GCP_CREDENTIAL_TYPE_UNSUPPORTED')
    if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]@' + re.escape(project) + r'\.iam\.gserviceaccount\.com', email) or expected_service_account and email != expected_service_account:
        raise CredentialError('GCP_SERVICE_ACCOUNT_SCOPE_MISMATCH')
    return identity

