"""Keystone provisioning: all passwords and tokens stay on the customer node."""
import json
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request
from .cli import ProviderError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Keystone:
    def __init__(self, endpoint, timeout=30, *, test_allow_http=False):
        parsed = urllib.parse.urlsplit(endpoint)
        schemes = ('https', 'http') if test_allow_http is True else ('https',)
        if (parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or any(c.isspace() for c in endpoint)
                or any(c in endpoint for c in ('$', '{', '}', '`'))):
            raise ValueError('auth_url must be an HTTPS Keystone v3 endpoint')
        self.endpoint = endpoint.rstrip('/')
        if not self.endpoint.endswith('/v3'):
            raise ValueError('auth_url must end with /v3')
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def request(self, method, path, body=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Auth-Token'] = token
        request = urllib.request.Request(self.endpoint + path,
            data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ProviderError('identity_response_too_large')
                return (json.loads(raw) if raw else {}, response.headers.get('X-Subject-Token'))
        except urllib.error.HTTPError as exc:
            code = {401:'unauthorized',403:'forbidden',404:'not_found',409:'conflict'}.get(exc.code, 'identity_failed')
            raise ProviderError(code) from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise ProviderError('identity_connection_or_response_failed') from None


def _segment(value):
    return urllib.parse.quote(str(value), safe='')


def _field(value, *path):
    try:
        for key in path:
            value = value[key]
        if not isinstance(value, str) or not value:
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError):
        raise ProviderError('identity_invalid_response') from None


def configure_identity(config, admin_password, on_resource=lambda resource: None, *, keystone_factory=Keystone,
                       on_intent=lambda operation, resource: None, on_credentials=lambda auth: None,
                       on_service_auth=lambda auth: None):
    personal = config.get('project_name') == 'railshot'
    required = ('auth_url', 'admin_username', 'user_domain_name', 'service_username')
    required += ('project_domain_name', 'role_name') if personal else ('project_id', 'role_id')
    for key in required:
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError('Missing identity setting: ' + key)
    if not admin_password:
        raise ValueError('Administrator password required')
    client = (keystone_factory(config['auth_url'], test_allow_http=True) if config.get('test_allow_http') is True
              else keystone_factory(config['auth_url']))
    password_auth = {'methods': ['password'], 'password': {'user': {
        'name': config['admin_username'], 'domain': {'name': config['user_domain_name']}, 'password': admin_password}}}
    # Obtain unscoped identity once; never persist this short-lived token.
    unscoped, _ = client.request('POST', '/auth/tokens', {'auth': {'identity': password_auth, 'scope': 'unscoped'}})
    domain_id = _field(unscoped, 'token','user','domain','id')
    admin_scope = config.get('admin_scope')
    if admin_scope is None:
        if personal:
            raise ValueError('Personal bootstrap requires an explicit administrator scope')
        admin_scope = {'project': {'id': config.get('admin_project_id', config['project_id'])}}
    if not isinstance(admin_scope, dict) or len(admin_scope) != 1 or not set(admin_scope) <= {'system','domain','project'}:
        raise ValueError('admin_scope must explicitly select system, domain or project')
    scoped, admin_token = client.request('POST', '/auth/tokens', {'auth': {'identity': password_auth,
        'scope': admin_scope}})
    if not isinstance(admin_token,str) or not admin_token:
        raise ProviderError('identity_invalid_response')
    config = dict(config)
    if personal:
        # Select the role before any mutation. Default member roles are the Nova
        # project-member policy baseline; custom cloud policies are checked later.
        role_name = config['role_name']
        if role_name.lower() not in ('member', '_member_'):
            raise ProviderError('compute_member_role_required')
        roles, _ = client.request('GET', '/roles?' + urllib.parse.urlencode({'name': role_name}), token=admin_token)
        matches = [r for r in roles.get('roles', []) if r.get('name') == role_name]
        if len(matches) != 1:
            raise ProviderError('compute_role_ambiguous_or_missing')
        config['role_id'] = _field(matches[0], 'id')
        project_domain_id = domain_id
        if config['project_domain_name'] != config['user_domain_name']:
            domains, _ = client.request('GET', '/domains?' + urllib.parse.urlencode({'name': config['project_domain_name']}), token=admin_token)
            matches = [d for d in domains.get('domains', []) if d.get('name') == config['project_domain_name']]
            if len(matches) != 1:
                raise ProviderError('project_domain_ambiguous_or_missing')
            project_domain_id = _field(matches[0], 'id')
        projects, _ = client.request('GET', '/projects?' + urllib.parse.urlencode(
            {'domain_id': project_domain_id, 'name': 'railshot'}), token=admin_token)
        if not isinstance(projects.get('projects'), list):
            raise ProviderError('identity_invalid_response')
        matches = [p for p in projects['projects'] if p.get('name') == 'railshot' and p.get('domain_id') == project_domain_id]
        if len(matches) > 1:
            raise ProviderError('railshot_project_ambiguous')
        created_project = not matches
        if matches:
            project = matches[0]
            if project.get('enabled') is False:
                raise ProviderError('railshot_project_disabled')
        else:
            if config.get('project_id'):
                raise ProviderError('project_mismatch')
            on_intent('create_project', {'type': 'project', 'name': 'railshot', 'domain_id': project_domain_id})
            response, _ = client.request('POST', '/projects', {'project': {'name': 'railshot',
                'domain_id': project_domain_id, 'enabled': True, 'description': 'Shared Railshot project'}}, token=admin_token)
            project = response.get('project', {})
        project_id = _field(project, 'id')
        if config.get('project_id') and config['project_id'] != project_id:
            raise ProviderError('project_mismatch')
        config['project_id'] = project_id
        on_resource({'type': 'project', 'id': project_id, 'name': 'railshot', 'domain_id': project_domain_id,
                     'created': created_project, 'shared': True, 'delete_on_remove': False})
    query = urllib.parse.urlencode({'domain_id': domain_id, 'name': config['service_username']})
    existing, _ = client.request('GET', '/users?' + query, token=admin_token)
    if not isinstance(existing.get('users'),list):
        raise ProviderError('identity_invalid_response')
    if existing.get('users'):
        raise ProviderError('existing_service_user', 'Existing service user is never adopted or reset; reconcile recorded resources first')
    service_password = secrets.token_urlsafe(48)
    on_intent('create_user', {'type': 'user', 'name': config['service_username'], 'domain_id': domain_id})
    created, _ = client.request('POST', '/users', {'user': {'name': config['service_username'],
        'domain_id': domain_id, 'password': service_password, 'enabled': True,
        'description': 'Railshot customer-local bootstrap account'}}, token=admin_token)
    user_id = _field(created,'user','id')
    on_resource({'type': 'user', 'id': user_id, 'name': config['service_username'], 'domain_id': domain_id})
    on_service_auth({'auth_url': config['auth_url'], 'user_id': user_id, 'password': service_password,
                     'project_id': config['project_id']})
    path = '/projects/{}/users/{}/roles/{}'.format(*map(_segment, (config['project_id'],user_id,config['role_id'])))
    on_intent('assign_role', {'type': 'role_assignment', 'user_id': user_id,
                            'project_id': config['project_id'], 'role_id': config['role_id']})
    client.request('PUT', path, token=admin_token)
    on_resource({'type': 'role_assignment', 'user_id': user_id, 'project_id': config['project_id'], 'role_id': config['role_id']})
    service, service_token = client.request('POST', '/auth/tokens', {'auth': {'identity': {
        'methods': ['password'], 'password': {'user': {'id': user_id, 'password': service_password}}},
        'scope': {'project': {'id': config['project_id']}}}})
    if not isinstance(service_token,str) or not service_token or _field(service,'token','project','id') != config['project_id']:
        raise ProviderError('project_mismatch')
    on_intent('create_application_credential', {'type': 'application_credential', 'user_id': user_id})
    credential, _ = client.request('POST', '/users/' + _segment(user_id) + '/application_credentials',
        {'application_credential': {'name': 'railshot-bootstrap', 'roles': [{'id': config['role_id']}], 'unrestricted': False}}, token=service_token)
    app = {'id':_field(credential,'application_credential','id'), 'secret':_field(credential,'application_credential','secret')}
    on_resource({'type': 'application_credential', 'id': app['id'], 'user_id': user_id})
    auth = {'auth_url': config['auth_url'], 'application_credential_id': app['id'], 'application_credential_secret': app['secret']}
    for option in ('region_name', 'interface'):
        if config.get(option):
            auth[option] = config[option]
    # Save the one-time secret immediately, even if subsequent verification fails.
    on_credentials(auth)
    verified, _ = client.request('POST', '/auth/tokens', {'auth': {'identity': {'methods': ['application_credential'],
        'application_credential': {'id': app['id'], 'secret': app['secret']}}}})
    if _field(verified,'token','project','id') != config['project_id']:
        raise ProviderError('project_mismatch')
    return auth
