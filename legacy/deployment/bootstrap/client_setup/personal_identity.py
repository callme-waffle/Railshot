"""Personal installer authentication using the existing administrator bootstrap.

Endpoint discovery reads only literal public endpoints. It never sources openrc,
executes configuration, or scans the network. Cloud writes have durable intent
records; an interrupted write must be reconciled instead of blindly replayed.
"""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shlex
import stat
import urllib.parse

from .credentials import CredentialStore
from .state import atomic_private_write, private_directory, read_private
from infrastructure.providers.openstack.cli import OpenStackCLI, ProviderError
from infrastructure.providers.openstack.identity import Keystone, configure_identity


def validate_endpoint(value, *, test_allow_http=False):
    # Constructor validates syntax and builds a transport; it sends no request.
    Keystone(value, test_allow_http=test_allow_http)
    return value.rstrip('/')


def discover_auth_urls(*, environ=None, paths=None, test_allow_http=False):
    """Return unique validated endpoints, with no configuration secrets attached."""
    environ = os.environ if environ is None else environ
    allowed_owners = {0, os.geteuid()}
    homes = [Path(environ.get('HOME', str(Path.home())))]
    # sudo normally changes HOME and may remove OS_AUTH_URL. Only accept a real
    # sudo identity pair, and inspect known configuration in that user's home.
    if os.geteuid() == 0 and environ.get('SUDO_USER') and environ.get('SUDO_UID', '').isdigit():
        try:
            original_user = pwd.getpwnam(environ['SUDO_USER'])
            if original_user.pw_uid == int(environ['SUDO_UID']):
                allowed_owners.add(original_user.pw_uid)
                homes.append(Path(original_user.pw_dir))
        except KeyError:
            pass
    if paths is None:
        paths = [path for home in homes for path in (home / '.config/openstack/clouds.yaml',
                 home / 'admin-openrc.sh', home / 'openrc')]
        paths += [Path('/etc/openstack/clouds.yaml'), Path('/etc/openstack/admin-openrc.sh'),
                  Path('/etc/kolla/admin-openrc.sh')]
        if environ.get('OS_CLIENT_CONFIG_FILE'):
            paths.insert(0, Path(environ['OS_CLIENT_CONFIG_FILE']))
    candidates = []

    def add(value):
        if not isinstance(value, str):
            return
        try:
            endpoint = validate_endpoint(value, test_allow_http=test_allow_http)
        except ValueError:
            return
        if endpoint not in candidates:
            candidates.append(endpoint)

    add(environ.get('OS_AUTH_URL'))
    for candidate in paths:
        path = Path(candidate)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid not in allowed_owners
                        or stat.S_IMODE(info.st_mode) & 0o022):
                    continue
                raw = stream.read(1048577)
            if len(raw) > 1048576:
                continue
            content = raw.decode('utf-8')
            if path.suffix in ('.yaml', '.yml', '.json'):
                import yaml
                data = yaml.safe_load(content)
                clouds = data.get('clouds', {}) if isinstance(data, dict) else {}
                if isinstance(clouds, dict):
                    for cloud in clouds.values():
                        auth = cloud.get('auth', {}) if isinstance(cloud, dict) else {}
                        if isinstance(auth, dict):
                            add(auth.get('auth_url'))
            else:
                for line in content.splitlines():
                    match = re.fullmatch(r'\s*(?:export\s+)?OS_AUTH_URL\s*=\s*(.*?)\s*', line)
                    if not match or any(c in match[1] for c in ('$', '`', ';', '\\')):
                        continue
                    words = shlex.split(match[1], comments=True)
                    if len(words) == 1:
                        add(words[0])
        except (OSError, UnicodeError, ValueError, ImportError):
            continue
        except Exception as exc:
            # PyYAML exception classes are unavailable if PyYAML is absent.
            if type(exc).__module__.startswith('yaml'):
                continue
            raise
    return candidates


def select_auth_url(*, input_fn=input, test_allow_http=False, environ=None, paths=None):
    candidates = discover_auth_urls(environ=environ, paths=paths, test_allow_http=test_allow_http)
    if len(candidates) == 1:
        print('로컬 설정에서 OpenStack 인증 주소를 찾았습니다: ' + candidates[0], flush=True)
        return candidates[0]
    if candidates:
        for index, endpoint in enumerate(candidates, 1):
            print(f'{index}. {endpoint}', flush=True)
        choice = input_fn('OpenStack 인증 주소 번호 또는 사용할 인증 주소를 입력하십시오: ').strip()
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return candidates[int(choice) - 1]
    else:
        choice = input_fn('OpenStack 인증 주소를 찾지 못했습니다. Keystone v3 주소를 입력하십시오: ').strip()
    return validate_endpoint(choice, test_allow_http=test_allow_http)


def _load(path, default):
    return json.loads(read_private(path)) if path.exists() or path.is_symlink() else default


def _save(path, data):
    atomic_private_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode())


def prepare_personal_identity(config_dir, state_dir, *, project_id=None, test_allow_http=False,
                              input_fn=input, password_fn=getpass.getpass,
                              keystone_factory=Keystone, cli_factory=OpenStackCLI):
    """Prepare encrypted auth and return auth/project_id/non-secret ownership.

    Caller holds the installation lock. Administrator passwords are never saved.
    Existing credentials are verified but never retroactively claimed as created.
    """
    config_dir, state_dir = private_directory(Path(config_dir)), private_directory(Path(state_dir))
    ownership_path = state_dir / 'identity-ownership.json'
    progress_path = state_dir / 'identity-progress.json'
    ownership = _load(ownership_path, {'version': 1, 'project_name': 'railshot', 'resources': []})
    progress = _load(progress_path, {'version': 1, 'status': 'new'})
    if ownership.get('version') != 1 or progress.get('version') != 1:
        raise ValueError('Unsupported identity state version')
    if progress.get('status') == 'pending':
        raise ProviderError('identity_prior_outcome_unknown')
    vault = CredentialStore(config_dir)
    owner_vault = CredentialStore(config_dir / 'identity-owner')

    if vault.path.exists() or vault.path.is_symlink():
        auth = vault.load()
        validate_endpoint(auth['auth_url'], test_allow_http=test_allow_http)
        if ownership.get('auth_url') and ownership['auth_url'] != auth['auth_url']:
            raise ProviderError('identity_binding_changed')
        recorded_credentials = [r['id'] for r in ownership['resources']
                                if r.get('type') == 'application_credential' and r.get('created') is True]
        if recorded_credentials and recorded_credentials != [auth.get('application_credential_id')]:
            raise ProviderError('identity_binding_changed')
    else:
        if any(r.get('created') for r in ownership['resources']):
            raise ProviderError('identity_partial_creation_requires_reconciliation')
        endpoint = select_auth_url(input_fn=input_fn, test_allow_http=test_allow_http)
        admin_username = input_fn('OpenStack 관리자 ID: ').strip()
        user_domain = input_fn('관리자 사용자 도메인 이름 [Default]: ').strip() or 'Default'
        admin_project = input_fn('관리자 인증 프로젝트 이름 [admin]: ').strip() or 'admin'
        admin_project_domain = input_fn('관리자 인증 프로젝트 도메인 [' + user_domain + ']: ').strip() or user_domain
        project_domain = input_fn('railshot 프로젝트 도메인 [' + admin_project_domain + ']: ').strip() or admin_project_domain
        service_default = 'railshot-' + secrets.token_hex(6)
        service_username = input_fn('새 전용 서비스 계정 이름 [' + service_default + ']: ').strip() or service_default
        role_name = input_fn('VM 운영용 프로젝트 구성원 역할 이름 [member]: ').strip() or 'member'
        config = {'auth_url': endpoint, 'admin_username': admin_username,
                  'user_domain_name': user_domain, 'project_name': 'railshot',
                  'project_domain_name': project_domain, 'service_username': service_username,
                  'role_name': role_name, 'test_allow_http': test_allow_http,
                  'admin_scope': {'project': {'name': admin_project, 'domain': {'name': admin_project_domain}}}}
        if project_id:
            config['project_id'] = project_id
        ownership['auth_url'] = endpoint
        ownership['test_allow_http'] = test_allow_http is True
        ownership['administrator'] = {key: config[key] for key in ('admin_username', 'user_domain_name', 'admin_scope')}
        _save(ownership_path, ownership)

        def intent(operation, resource):
            progress.update(status='pending', operation=operation, resource=resource)
            _save(progress_path, progress)

        def resource(value):
            entry = dict(value)
            entry.setdefault('created', True)
            if entry['type'] == 'project':
                ownership['project_id'] = entry['id']
            elif entry['type'] == 'user':
                entry['delete_on_remove'] = False
            elif entry['type'] == 'application_credential':
                entry.update(unrestricted=False, revocation_requires='created_service_user_password')
            ownership['resources'].append(entry)
            _save(ownership_path, ownership)
            progress.update(status='recorded')
            _save(progress_path, progress)

        password = password_fn('OpenStack 관리자 암호 (저장하지 않습니다): ')
        try:
            auth = configure_identity(config, password, resource, keystone_factory=keystone_factory,
                                      on_intent=intent, on_credentials=vault.save,
                                      on_service_auth=owner_vault.save)
        finally:
            del password

    cli = cli_factory(auth)
    token = cli.run(['token', 'issue'])
    verified_project = token.get('project_id')
    if not isinstance(verified_project, str) or not verified_project:
        raise ProviderError('identity_invalid_response')
    for expected in (project_id, ownership.get('project_id')):
        if expected and expected != verified_project:
            raise ProviderError('project_mismatch')
    if not ownership.get('project_id'):
        # An older vault has no creation evidence: verify its project and retain
        # the credential as externally owned instead of adopting it for deletion.
        project = cli.run(['project', 'show', verified_project])
        if project.get('name') != 'railshot':
            raise ProviderError('railshot_project_required')
        ownership.update(project_id=verified_project, auth_url=auth['auth_url'])
        ownership['resources'].append({'type': 'application_credential',
            'id': auth['application_credential_id'], 'created': False, 'delete_on_remove': False})
    # Checks actual compute authorization; this is not evidence of VM creation.
    cli.run(['server', 'list'])
    ownership['verification'] = {'project': True, 'compute_list': True, 'compute_create': False}
    _save(ownership_path, ownership)
    progress.update(status='complete')
    _save(progress_path, progress)
    return {'auth': auth, 'project_id': verified_project, 'ownership': ownership}


def revoke_personal_identity(config_dir, state_dir, *, test_allow_http=False, keystone_factory=Keystone):
    """Revoke only this installation's credential, using its generated owner.

    No administrator password, unrestricted credential, user deletion, or project
    deletion is involved. Caller retains local vaults on error, and removes them
    after a verified success. GET before DELETE reconciles interrupted requests.
    """
    config_dir, state_dir = Path(config_dir), Path(state_dir)
    ownership = _load(state_dir / 'identity-ownership.json', {'resources': []})
    credentials = [r for r in ownership['resources'] if r.get('type') == 'application_credential'
                   and r.get('created') is True]
    if not credentials:
        return {'application_credential': 'externally_owned', 'retained': ['external_identity']}
    if len(credentials) != 1:
        raise ProviderError('identity_ownership_ambiguous')
    credential = credentials[0]
    users = [r for r in ownership['resources'] if r.get('type') == 'user'
             and r.get('created') is True and r.get('id') == credential.get('user_id')]
    if len(users) != 1:
        raise ProviderError('identity_owner_not_created')
    receipt_path = state_dir / 'identity-revocation.json'
    receipt = _load(receipt_path, {})
    binding = {'user_id': users[0]['id'], 'application_credential_id': credential['id'],
               'project_id': ownership['project_id'], 'auth_url': ownership['auth_url']}
    if receipt and any(receipt.get(key) != value for key, value in binding.items()):
        raise ProviderError('identity_revocation_binding_changed')
    if receipt.get('status') == 'revoked':
        return {'application_credential': 'revoked', 'retained': ['service_user', 'role_assignment', 'shared_project']}
    auth = CredentialStore(config_dir).load()
    owner = CredentialStore(config_dir / 'identity-owner').load()
    if (auth.get('application_credential_id') != credential['id']
            or auth.get('auth_url') != binding['auth_url']
            or any(owner.get(key) != binding[key] for key in ('user_id', 'project_id', 'auth_url'))):
        raise ProviderError('identity_revocation_binding_changed')
    validate_endpoint(owner['auth_url'], test_allow_http=test_allow_http)
    client = (keystone_factory(owner['auth_url'], test_allow_http=True) if test_allow_http is True
              else keystone_factory(owner['auth_url']))
    response, token = client.request('POST', '/auth/tokens', {'auth': {'identity': {'methods': ['password'],
        'password': {'user': {'id': owner['user_id'], 'password': owner['password']}}},
        'scope': {'project': {'id': owner['project_id']}}}})
    if (not token or response.get('token', {}).get('project', {}).get('id') != binding['project_id']
            or response.get('token', {}).get('user', {}).get('id') != binding['user_id']):
        raise ProviderError('identity_revocation_binding_changed')
    path = '/users/{}/application_credentials/{}'.format(
        urllib.parse.quote(binding['user_id'], safe=''), urllib.parse.quote(credential['id'], safe=''))

    def present():
        try:
            data, _ = client.request('GET', path, token=token)
            if data.get('application_credential', {}).get('id') != credential['id']:
                raise ProviderError('identity_invalid_response')
            return True
        except ProviderError as exc:
            if exc.code == 'not_found':
                return False
            raise

    if present():
        _save(receipt_path, {**binding, 'status': 'pending'})
        try:
            client.request('DELETE', path, token=token)
        except ProviderError as exc:
            if exc.code != 'not_found':
                raise
        if present():
            raise ProviderError('identity_revocation_unverified')
    _save(receipt_path, {**binding, 'status': 'revoked'})
    return {'application_credential': 'revoked', 'retained': ['service_user', 'role_assignment', 'shared_project']}
