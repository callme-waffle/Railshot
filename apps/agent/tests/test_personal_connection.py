"""Customer connection contract: real vault crypto and restart-safe enrollment.

Cloud writes and OS changes remain fakes. These tests do not replace a real
WireGuard handshake or SSH connection against the designated OpenStack host.
"""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


ROOT = Path(__file__).resolve().parents[3]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


client = module('personal_connection_client', 'apps/agent/personal.py')
control = module('personal_connection_control', 'apps/agent/openstack_control.py')


@pytest.fixture
def connection(tmp_path, monkeypatch):
    from client_setup import personal_identity
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public = key.public_key()
    pem = public.public_bytes(serialization.Encoding.PEM,
                              serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    fingerprint = hashlib.sha256(public.public_bytes(serialization.Encoding.DER,
                              serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
    for name in ('CONFIG', 'STATE'):
        directory = tmp_path / name.lower()
        directory.mkdir(mode=0o700)
        monkeypatch.setattr(client, name, directory)
    config = {'target_id': 'personal-11111111-1111-4111-8111-111111111111',
              'generation': 1, 'client_token': 'test-client-credential',
              'api_url': 'http://127.0.0.1:8080', 'test_allow_http': True,
              'vault_public_key': pem, 'vault_key_id': fingerprint}
    writes, posts = [], []

    class Cloud:
        def __init__(self, endpoint, **options):
            assert endpoint == 'http://127.0.0.1/identity/v3'
            assert options == {'test_allow_http': True}

        def request(self, method, path, body=None, token=None):
            if path == '/auth/tokens':
                return {'token': {'user': {'id': 'service-user', 'domain': {'id': 'default'}},
                                   'project': {'id': 'project1'}}}, 'test-identity-token'
            if path.startswith('/roles?'):
                return {'roles': [{'id': 'member-role', 'name': 'member'}]}, None
            if path.startswith('/projects?'):
                return {'projects': [{'id': 'project1', 'name': 'railshot', 'domain_id': 'default'}]}, None
            if path.startswith('/users?'):
                return {'users': []}, None
            writes.append((method, path))
            if path == '/users':
                assert body['user']['password'] != 'test-admin-password'
                return {'user': {'id': 'service-user'}}, None
            if method == 'PUT':
                return {}, None
            if path == '/users/service-user/application_credentials':
                assert body['application_credential']['unrestricted'] is False
                assert body['application_credential']['roles'] == [{'id': 'member-role'}]
                return {'application_credential': {'id': 'app-credential',
                                                    'secret': 'test-app-secret'}}, None
            raise AssertionError((method, path))

    class CLI:
        def __init__(self, auth):
            assert auth['application_credential_secret'] == 'test-app-secret'

        def run(self, args):
            if args == ['token', 'issue']:
                return {'project_id': 'project1'}
            assert args == ['server', 'list']
            return []

    prepare = personal_identity.prepare_personal_identity
    answers = iter(['admin', '', '', '', '', 'railshot-service', ''])
    monkeypatch.setattr(personal_identity, 'select_auth_url',
                        lambda **options: 'http://127.0.0.1/identity/v3')
    monkeypatch.setattr(personal_identity, 'prepare_personal_identity',
                        lambda *args, **options: prepare(*args, **options,
                            input_fn=lambda _: next(answers), password_fn=lambda _: 'test-admin-password',
                            keystone_factory=Cloud, cli_factory=CLI))
    monkeypatch.setattr(client, 'api',
                        lambda *args, **options: posts.append((args, options)) or {})
    return SimpleNamespace(config=config, key=key, writes=writes, posts=posts,
                           identity=personal_identity, tmp=tmp_path)


def unwrap(key, document):
    return key.decrypt(base64.b64decode(document['wrapped_dek'], validate=True),
                       padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                    algorithm=hashes.SHA256(), label=None))


def test_identity_creates_encrypted_vault_and_resume_reposts_same_binding(connection, monkeypatch):
    client.finish_identity(connection.config, SimpleNamespace(project_id=None))
    vault = client.AssistedCredentialStore(client.CONFIG)
    owner = client.AssistedCredentialStore(client.CONFIG / 'identity-owner')
    document = vault.document()
    owner_document = owner.document()
    assert document['nonce'] != owner_document['nonce']
    dek = unwrap(connection.key, document)
    assert dek == unwrap(connection.key, owner_document)
    assert len(dek) == 32
    assert vault.unlock(base64.b64encode(dek).decode(), target_id=connection.config['target_id'],
                        generation=1, project_id='project1')['application_credential_secret'] == 'test-app-secret'
    assert not (client.CONFIG / 'credentials.key').exists()
    assert not (client.CONFIG / 'credentials.enc').exists()
    for directory in (client.CONFIG, client.STATE):
        for path in directory.rglob('*'):
            if path.is_file():
                assert path.stat().st_mode & 0o077 == 0
                text = path.read_text()
                assert 'test-admin-password' not in text
                assert 'test-app-secret' not in text
                assert base64.b64encode(dek).decode() not in text
    assert connection.posts[0][0][3] == vault.metadata()
    assert connection.posts[0][1] == {'test_allow_http': True}
    writes = list(connection.writes)
    monkeypatch.setattr(connection.identity, 'prepare_personal_identity',
                        lambda *args, **options: pytest.fail('Do not recreate identity on resume'))
    client.finish_identity(connection.config, SimpleNamespace(project_id=None))
    assert connection.writes == writes
    assert connection.posts[1][0][3] == connection.posts[0][0][3]


def test_upload_failure_retains_encrypted_identity_for_retry(connection, monkeypatch):
    posts = client.api
    monkeypatch.setattr(client, 'api', lambda *args, **options: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(TimeoutError):
        client.finish_identity(connection.config, SimpleNamespace(project_id=None))
    metadata = client.AssistedCredentialStore(client.CONFIG).metadata()
    assert json.loads((client.CONFIG / 'client.json').read_text())['project_id'] == 'project1'
    assert json.loads((client.STATE / 'identity-progress.json').read_text())['status'] == 'complete'
    writes = list(connection.writes)
    monkeypatch.setattr(client, 'api', posts)
    client.finish_identity(connection.config, SimpleNamespace(project_id=None))
    assert connection.writes == writes
    assert connection.posts[0][0][3] == metadata


def test_backend_unlock_runs_one_read_and_rejects_replay_before_decrypt(connection):
    client.finish_identity(connection.config, SimpleNamespace(project_id=None))
    store = client.AssistedCredentialStore(client.CONFIG)
    unlock_key = base64.b64encode(unwrap(connection.key, store.document())).decode()
    home = connection.tmp / 'command'; home.mkdir(mode=0o700)
    request = {'version': 1, 'job_id': 'read1', 'action': 'openstack.execute',
               'target_id': connection.config['target_id'], 'generation': 1,
               'unlock_key': unlock_key, 'params': {'argv': ['server', 'list']}}
    auth_reads, cloud_calls = [], []

    def loader(*args, **options):
        auth_reads.append(True)
        return store.unlock(*args, **options)

    def cloud_process(argv, **options):
        cloud_calls.append(argv[3:5])
        path = Path(options['env']['OS_CLIENT_CONFIG_FILE'])
        assert str(path).startswith('/dev/shm/railshot-openstack-')
        assert path.stat().st_mode & 0o077 == 0
        assert json.loads(path.read_text())['clouds']['railshot']['auth']['application_credential_secret'] == 'test-app-secret'
        assert 'test-app-secret' not in ' '.join(argv)
        result = {'project_id': 'project1', 'id': 'test-issued-token'} if argv[3:5] == ['token', 'issue'] else []
        return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr='')

    factory = lambda auth: control.OpenStackCLI(auth, runner=cloud_process, temp_parent='/dev/shm')
    response = control.execute_raw(json.dumps(request).encode(), control.COMMAND, loader,
                                  connection.config, cli_factory=factory, home=home)
    assert response['ok'] and response['result'] == []
    assert cloud_calls == [['token', 'issue'], ['server', 'list']]
    assert all(secret not in json.dumps(response) for secret in ('test-app-secret', 'test-issued-token', unlock_key))
    repeated = control.execute_raw(json.dumps(request).encode(), control.COMMAND, loader,
                                  connection.config, cli_factory=factory, home=home)
    assert repeated['ok'] is False
    assert len(auth_reads) == 1
    assert cloud_calls == [['token', 'issue'], ['server', 'list']]


def test_partial_identity_with_unknown_outcome_never_uploads_vault(connection):
    client.finish_identity(connection.config, SimpleNamespace(project_id=None))
    progress = client.STATE / 'identity-progress.json'
    progress.write_text(json.dumps({'version': 1, 'status': 'pending'})); progress.chmod(0o600)
    connection.posts.clear()
    with pytest.raises(ValueError, match='IDENTITY_PRIOR_OUTCOME_UNKNOWN'):
        client.finish_identity(connection.config, SimpleNamespace(project_id=None))
    assert connection.posts == []
