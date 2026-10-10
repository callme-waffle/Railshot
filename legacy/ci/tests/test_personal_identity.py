import importlib
import json
from pathlib import Path
import sys
import urllib.parse

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
bootstrap = importlib.import_module('deployment.bootstrap.client_setup.personal_identity')
identity = importlib.import_module('infrastructure.providers.openstack.identity')
ProviderError = bootstrap.ProviderError


class Cloud:
    def __init__(self):
        self.calls = []
        self.projects = [{'id': 'p', 'name': 'railshot', 'domain_id': 'd'}]
        self.existing_users = []
        self.fail = None
        self.deleted = False

    def factory(self, endpoint, **kwargs):
        self.endpoint = endpoint
        return self

    def request(self, method, path, body=None, token=None):
        self.calls.append((method, path, body))
        if self.fail == (method, path):
            raise ProviderError('timeout')
        if path == '/auth/tokens':
            return {'token': {'user': {'id': 'u', 'domain': {'id': 'd'}}, 'project': {'id': 'p'}}}, 'memory-token'
        if path.startswith('/roles?'):
            return {'roles': [{'id': 'r', 'name': 'member'}]}, None
        if path.startswith('/domains?'):
            return {'domains': [{'id': 'different-domain', 'name': 'Other'}]}, None
        if path.startswith('/projects?'):
            return {'projects': self.projects}, None
        if path.startswith('/users?'):
            return {'users': self.existing_users}, None
        if path == '/projects':
            return {'project': {'id': 'p'}}, None
        if path == '/users':
            return {'user': {'id': 'u'}}, None
        if method == 'PUT':
            return {}, None
        if path == '/users/u/application_credentials':
            return {'application_credential': {'id': 'a', 'secret': 'app-secret'}}, None
        if path == '/users/u/application_credentials/a':
            if self.deleted:
                raise ProviderError('not_found')
            if method == 'DELETE':
                self.deleted = True
                return {}, None
            return {'application_credential': {'id': 'a'}}, None
        raise AssertionError((method, path))


class CLI:
    def __init__(self, auth):
        self.auth = auth

    def run(self, args):
        if args == ['token', 'issue']:
            return {'project_id': 'p'}
        if args[:2] == ['project', 'show']:
            return {'name': 'railshot'}
        assert args == ['server', 'list']
        return []


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, 'select_auth_url', lambda **kwargs: 'https://cloud.example/v3')
    cloud = Cloud()
    answers = iter(['admin', '', '', '', '', 'railshot-service', ''])
    kwargs = dict(input_fn=lambda prompt: next(answers), password_fn=lambda prompt: 'admin-secret',
                  keystone_factory=cloud.factory, cli_factory=CLI)
    return tmp_path / 'config', tmp_path / 'state', cloud, kwargs


def test_prepare_and_reuse_scoped_existing_project_without_recreating(setup):
    config, state, cloud, kwargs = setup
    result = bootstrap.prepare_personal_identity(config, state, **kwargs)
    assert result['project_id'] == 'p'
    assert not any(method == 'POST' and path == '/projects' for method, path, body in cloud.calls)
    assert result['ownership']['resources'][0]['created'] is False
    assert result['ownership']['resources'][0]['delete_on_remove'] is False
    assert result['ownership']['verification']['compute_create'] is False
    assert 'password' not in result['auth']
    for path in state.iterdir():
        assert all(secret not in path.read_text() for secret in ('admin-secret', 'app-secret', 'memory-token'))
        assert path.stat().st_mode & 0o777 == 0o600
    assert bootstrap.CredentialStore(config / 'identity-owner').load()['user_id'] == 'u'
    cloud.calls.clear()
    assert bootstrap.prepare_personal_identity(config, state, **kwargs)['project_id'] == 'p'
    assert cloud.calls == []


def test_create_project_records_before_user(setup):
    config, state, cloud, kwargs = setup
    cloud.projects = []
    result = bootstrap.prepare_personal_identity(config, state, **kwargs)
    assert result['ownership']['resources'][0]['created'] is True
    writes = [(method, path) for method, path, body in cloud.calls if method in ('POST', 'PUT') and path != '/auth/tokens']
    assert writes == [('POST', '/projects'), ('POST', '/users'), ('PUT', '/projects/p/users/u/roles/r'),
                      ('POST', '/users/u/application_credentials')]


@pytest.mark.parametrize('failure', [('POST', '/projects'), ('POST', '/users'), ('PUT', '/projects/p/users/u/roles/r'),
                                   ('POST', '/users/u/application_credentials')])
def test_unknown_mutation_never_repeated(setup, failure):
    config, state, cloud, kwargs = setup
    cloud.projects = []
    cloud.fail = failure
    with pytest.raises(ProviderError, match='timeout'):
        bootstrap.prepare_personal_identity(config, state, **kwargs)
    progress = json.loads((state / 'identity-progress.json').read_text())
    assert progress['status'] == 'pending'
    cloud.calls.clear()
    with pytest.raises(ProviderError, match='identity_prior_outcome_unknown'):
        bootstrap.prepare_personal_identity(config, state, **kwargs)
    assert cloud.calls == []


def test_unrelated_service_user_never_adopted_or_reset(setup):
    config, state, cloud, kwargs = setup
    cloud.existing_users = [{'id': 'someone-else'}]
    with pytest.raises(ProviderError, match='Existing service user'):
        bootstrap.prepare_personal_identity(config, state, **kwargs)
    assert not any(method in ('PUT', 'DELETE', 'PATCH') or path == '/users' for method, path, body in cloud.calls)


def test_same_name_projects_in_other_domains_are_not_adopted(setup):
    config, state, cloud, kwargs = setup
    cloud.projects = [{'id': 'other', 'name': 'railshot', 'domain_id': 'other-domain'}]
    result = bootstrap.prepare_personal_identity(config, state, **kwargs)
    assert result['ownership']['resources'][0]['id'] == 'p'
    assert result['ownership']['resources'][0]['created'] is True


def test_duplicate_projects_abort_before_mutation(setup):
    config, state, cloud, kwargs = setup
    cloud.projects += [{'id': 'duplicate', 'name': 'railshot', 'domain_id': 'd'}]
    with pytest.raises(ProviderError, match='railshot_project_ambiguous'):
        bootstrap.prepare_personal_identity(config, state, **kwargs)
    assert all(method == 'GET' or path == '/auth/tokens' for method, path, body in cloud.calls)


def test_supplied_project_must_match(setup):
    config, state, cloud, kwargs = setup
    with pytest.raises(ProviderError, match='project_mismatch'):
        bootstrap.prepare_personal_identity(config, state, project_id='wrong', **kwargs)
    assert not any(path == '/users' for method, path, body in cloud.calls)


def test_admin_role_is_not_accepted(setup):
    config, state, cloud, kwargs = setup
    answers = iter(['admin', '', '', '', '', 'service', 'admin'])
    kwargs['input_fn'] = lambda prompt: next(answers)
    with pytest.raises(ProviderError, match='compute_member_role_required'):
        bootstrap.prepare_personal_identity(config, state, **kwargs)
    assert not any(path in ('/users', '/projects') for method, path, body in cloud.calls)


def test_credential_saved_before_verification_failure(setup):
    config, state, cloud, kwargs = setup
    request = cloud.request
    def failing(method, path, body=None, token=None):
        if body and body.get('auth', {}).get('identity', {}).get('methods') == ['application_credential']:
            raise ProviderError('timeout')
        return request(method, path, body, token)
    cloud.request = failing
    with pytest.raises(ProviderError, match='timeout'):
        bootstrap.prepare_personal_identity(config, state, **kwargs)
    assert bootstrap.CredentialStore(config).load()['application_credential_secret'] == 'app-secret'
    assert bootstrap.prepare_personal_identity(config, state, **kwargs)['project_id'] == 'p'


def test_revoke_only_created_credential_and_keep_user_project(setup):
    config, state, cloud, kwargs = setup
    bootstrap.prepare_personal_identity(config, state, **kwargs)
    cloud.calls.clear()
    result = bootstrap.revoke_personal_identity(config, state, keystone_factory=cloud.factory)
    assert result['application_credential'] == 'revoked'
    assert [path for method, path, body in cloud.calls if method == 'DELETE'] == ['/users/u/application_credentials/a']
    password = cloud.calls[0][2]['auth']['identity']['password']['user']['password']
    assert password != 'admin-secret'
    assert 'shared_project' in result['retained']
    cloud.calls.clear()
    bootstrap.revoke_personal_identity(config, state, keystone_factory=cloud.factory)
    assert cloud.calls == []


def test_revoke_lost_response_reconciles_without_repeated_delete(setup):
    config, state, cloud, kwargs = setup
    bootstrap.prepare_personal_identity(config, state, **kwargs)
    request = cloud.request
    def lost_response(method, path, body=None, token=None):
        result = request(method, path, body, token)
        if method == 'DELETE':
            raise ProviderError('timeout')
        return result
    cloud.request = lost_response
    with pytest.raises(ProviderError, match='timeout'):
        bootstrap.revoke_personal_identity(config, state, keystone_factory=cloud.factory)
    assert bootstrap.CredentialStore(config).path.exists()
    cloud.calls.clear()
    assert bootstrap.revoke_personal_identity(config, state, keystone_factory=cloud.factory)['application_credential'] == 'revoked'
    assert not any(method == 'DELETE' for method, path, body in cloud.calls)


def test_preexisting_vault_remains_external(setup):
    config, state, cloud, kwargs = setup
    bootstrap.CredentialStore(config).save({'auth_url': 'https://cloud.example/v3',
        'application_credential_id': 'external', 'application_credential_secret': 'secret'})
    result = bootstrap.prepare_personal_identity(config, state, **kwargs)
    assert result['ownership']['resources'][0]['created'] is False
    assert bootstrap.revoke_personal_identity(config, state, keystone_factory=cloud.factory)['application_credential'] == 'externally_owned'
    assert cloud.calls == []


def test_endpoint_discovery_never_executes_openrc(tmp_path):
    marker = tmp_path / 'executed'
    openrc = tmp_path / 'openrc'
    openrc.write_text(f'export OS_AUTH_URL="$(touch {marker})"\nexport OS_AUTH_URL=https://cloud.example/v3\n'
                      'export OS_PASSWORD="never-display"\n')
    clouds = tmp_path / 'clouds.yaml'
    clouds.write_text('clouds:\n  one:\n    auth:\n      auth_url: https://other.example/v3\n      password: secret\n')
    assert bootstrap.discover_auth_urls(environ={}, paths=[openrc, clouds]) == ['https://cloud.example/v3', 'https://other.example/v3']
    assert not marker.exists()


def test_ambiguous_url_requires_selection(tmp_path):
    cloud = tmp_path / 'clouds.yaml'
    cloud.write_text('clouds:\n  one:\n    auth:\n      auth_url: https://other.example/v3\n')
    assert bootstrap.select_auth_url(environ={'OS_AUTH_URL': 'https://cloud.example/v3'}, paths=[cloud],
                                      input_fn=lambda prompt: '2') == 'https://other.example/v3'


def test_http_only_with_explicit_test_flag():
    with pytest.raises(ValueError):
        bootstrap.validate_endpoint('http://cloud.example/v3')
    assert bootstrap.validate_endpoint('http://cloud.example/v3', test_allow_http=True) == 'http://cloud.example/v3'
    with pytest.raises(ValueError):
        bootstrap.validate_endpoint('http://cloud.example/v3', test_allow_http=1)
    for value in ('https://u:secret@cloud/v3', 'https://cloud/v3?password=x', 'https://cloud/v3\n'):
        with pytest.raises(ValueError):
            bootstrap.validate_endpoint(value)


def test_explicit_target_domain_is_resolved_without_adopting_default(setup):
    config, state, cloud, kwargs = setup
    answers = iter(['admin', 'Default', '', '', 'Other', 'service', ''])
    kwargs['input_fn'] = lambda prompt: next(answers)
    bootstrap.prepare_personal_identity(config, state, **kwargs)
    queries = [urllib.parse.parse_qs(path.split('?', 1)[1]) for method, path, body in cloud.calls
               if path.startswith('/projects?')]
    assert queries == [{'domain_id': ['different-domain'], 'name': ['railshot']}]
    creation = [body for method, path, body in cloud.calls if method == 'POST' and path == '/projects']
    assert creation[0]['project']['domain_id'] == 'different-domain'


def test_owner_vault_mismatch_never_revokes(setup):
    config, state, cloud, kwargs = setup
    bootstrap.prepare_personal_identity(config, state, **kwargs)
    store = bootstrap.CredentialStore(config / 'identity-owner')
    owner = store.load()
    owner['user_id'] = 'unrelated'
    store.save(owner)
    cloud.calls.clear()
    with pytest.raises(ProviderError, match='identity_revocation_binding_changed'):
        bootstrap.revoke_personal_identity(config, state, keystone_factory=cloud.factory)
    assert cloud.calls == []


def test_changed_vault_credential_cannot_keep_old_ownership(setup):
    config, state, cloud, kwargs = setup
    bootstrap.prepare_personal_identity(config, state, **kwargs)
    store = bootstrap.CredentialStore(config)
    auth = store.load()
    auth['application_credential_id'] = 'external'
    store.save(auth)
    with pytest.raises(ProviderError, match='identity_binding_changed'):
        bootstrap.prepare_personal_identity(config, state, **kwargs)


def test_endpoint_discovery_ignores_writable_symlink_and_unsafe_yaml(tmp_path):
    cloud = tmp_path / 'clouds.yaml'
    cloud.write_text('clouds: {one: {auth: {auth_url: https://cloud.example/v3}}}')
    cloud.chmod(0o666)
    link = tmp_path / 'link.yaml'
    link.symlink_to(cloud)
    unsafe = tmp_path / 'unsafe.yaml'
    unsafe.write_text('!!python/object/apply:os.system ["touch should-never-exist"]')
    assert bootstrap.discover_auth_urls(environ={}, paths=[cloud, link, unsafe]) == []


def test_sudo_original_home_known_config_is_discovered(tmp_path, monkeypatch):
    from types import SimpleNamespace
    home = tmp_path / 'original'
    (home / '.config/openstack').mkdir(parents=True)
    (home / '.config/openstack/clouds.yaml').write_text('clouds: {one: {auth: {auth_url: https://sudo-cloud.example/v3}}}')
    monkeypatch.setattr(bootstrap.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(bootstrap.pwd, 'getpwnam', lambda user: SimpleNamespace(pw_uid=__import__('os').getuid(), pw_dir=str(home)))
    result = bootstrap.discover_auth_urls(environ={'HOME': str(tmp_path / 'root'), 'SUDO_USER': 'operator',
                                                  'SUDO_UID': str(__import__('os').getuid())})
    assert 'https://sudo-cloud.example/v3' in result


def test_cloud_template_url_is_not_an_endpoint_candidate(tmp_path):
    cloud = tmp_path / 'clouds.yaml'
    cloud.write_text('clouds: {one: {auth: {auth_url: "https://${KEYSTONE_HOST}/v3"}}}')
    assert bootstrap.discover_auth_urls(environ={}, paths=[cloud]) == []
