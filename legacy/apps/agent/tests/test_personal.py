import base64
import builtins
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[3]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


client = module('personal_test_client', 'apps/agent/personal.py')
remove = module('personal_test_remove', 'apps/agent/personal_remove.py')
gateway = module('personal_test_gateway', 'deployment/scripts/personal_wireguard.py')
KEY = base64.b64encode(bytes(range(32))).decode()
KEY2 = base64.b64encode(bytes(range(1, 33))).decode()


@pytest.fixture
def local(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    for name in ('CONFIG', 'STATE', 'PAYLOAD'):
        directory = tmp_path / name.lower()
        directory.mkdir(mode=0o700)
        monkeypatch.setattr(client, name, directory)
        monkeypatch.setattr(remove, name, directory)
    for name in ('UNIT', 'RUNTIME_UNIT', 'WG'):
        path = tmp_path / name.lower()
        monkeypatch.setattr(client, name, path)
        monkeypatch.setattr(remove, name, path)
    monkeypatch.setattr(remove, 'CLI_HOME', tmp_path / 'cli-home')
    for name in ('KUBE_UNIT', 'KUBE_PROXY_UNIT', 'KUBE_HOME', 'KUBE_WRAPPER', 'KUBE_SUDOERS'):
        monkeypatch.setattr(remove, name, tmp_path / name.lower())
    monkeypatch.setattr(remove, 'PID_FILES', (tmp_path / 'runtime.pid', tmp_path / 'kube.pid'))
    monkeypatch.setattr(remove, 'RUN', tmp_path)
    return tmp_path


def configuration():
    return {'target_id': 'target1', 'generation': 1, 'client_token': 'secret', 'api_url': 'https://example.test',
            'tunnel': {'server_public_key': KEY, 'endpoint': 'example.test:51820',
                       'address': '10.99.0.2/32', 'allowed_ips': '10.99.0.1/32'}}


def test_tunnel_rejects_transit_route():
    config = configuration()['tunnel']
    assert client.validate_tunnel(config) == ('10.99.0.2/32', '10.99.0.1/32')
    for route in ('0.0.0.0/0', '10.99.0.0/24', '10.99.0.2/32'):
        with pytest.raises(ValueError):
            client.validate_tunnel({**config, 'allowed_ips': route})


@pytest.mark.parametrize('url', ['http://example.test', 'https://user:pass@example.test', 'https://example.test/?token=x', 'https://example.test/#x'])
def test_no_credentials_redirect_or_plaintext_url(url):
    with pytest.raises(ValueError):
        client.https_url(url)


def test_removal_rejects_nonempty_applications(local):
    with pytest.raises(ValueError, match='APPLICATIONS_NOT_REMOVED'):
        client.handle_command(configuration(), {'id': 'operation1', 'kind': 'environment.delete', 'applications': ['app1'], 'delete_data': True})
    assert list(client.STATE.iterdir()) == []


def test_command_is_durable_and_not_replayed_after_unknown(local):
    receipts, launches = [], []
    command = {'id': 'operation1', 'kind': 'environment.delete', 'applications': [], 'delete_data': True}
    config = configuration()
    client.handle_command(config, command, sender=lambda c, r: receipts.append(r), launch=lambda *a: launches.append(a))
    client.handle_command(config, command, sender=lambda c, r: receipts.append(r), launch=lambda *a: launches.append(a))
    assert len(launches) == 1
    assert [r['status'] for r in receipts] == ['running', 'unknown']
    assert all(not r['client_removed'] for r in receipts)
    with pytest.raises(ValueError, match='JOB_ID_CONFLICT'):
        client.handle_command(config, {**command, 'delete_data': False})


def test_wrong_generation_does_not_write(local):
    with pytest.raises(ValueError, match='GENERATION_MISMATCH'):
        client.handle_command(configuration(), {'id': 'op', 'kind': 'environment.delete', 'applications': [], 'delete_data': True, 'generation': 2})
    assert list(client.STATE.iterdir()) == []


def prepare_owned():
    config = configuration()
    (remove.PAYLOAD / '.railshot-personal.json').write_text('{"version":1,"files":{}}')
    (remove.CONFIG / 'client.json').write_text(json.dumps(config))
    for path in (remove.UNIT, remove.WG):
        path.write_text(remove.MARKER + 'owned')
    return config


def test_removal_never_adopts_foreign_file(local):
    config = prepare_owned()
    (remove.CONFIG / 'customer-secret').write_text('keep')
    commands = []
    with pytest.raises(ValueError):
        remove.cleanup(config, runner=lambda args: commands.append(args))
    assert commands == []
    assert (remove.CONFIG / 'customer-secret').exists()


def test_removal_verifies_tunnel_and_preserves_unowned_sibling(local):
    config = prepare_owned()
    customer = local / 'customer-service'
    customer.write_text('keep')
    commands = []
    def runner(args):
        commands.append(args)
        return 'inactive' if 'show' in args and 'systemctl' in args else ''
    steps = remove.cleanup(config, runner=runner)
    assert len(steps) == 3
    assert customer.read_text() == 'keep'
    assert not remove.PAYLOAD.exists() and not remove.CONFIG.exists() and not remove.WG.exists()
    assert not any('openstack' in args or 'k3s' in args for args in commands)


def test_failed_tunnel_stop_is_not_success(local):
    config = prepare_owned()
    def runner(args):
        return 'inactive' if args[0] == 'systemctl' and 'show' in args else 'railshot0'
    with pytest.raises(ValueError):
        remove.cleanup(config, runner=runner)
    assert remove.PAYLOAD.exists()


def gateway_config(tmp_path):
    secret = tmp_path / 'key'
    secret.write_text(KEY)
    secret.chmod(0o600)
    return {'version': 1, 'interface': 'railshotwg', 'address_pool': '10.99.0.0/24',
            'server_address': '10.99.0.1/32', 'endpoint': 'example.test:51820', 'listen_port': 51820,
            'private_key_file': str(secret), 'ledger_path': str(tmp_path / 'ledger.json')}


def request(action='register', target='target1', public=KEY):
    return {'action': action, 'target_id': target, 'generation': 1, 'public_key': public}


def fake_gateway(args, **kwargs):
    return KEY if args[1] == 'pubkey' else KEY + '\t' + str(int(time.time()))


def test_gateway_idempotent_isolated_and_revoked(tmp_path):
    config = gateway_config(tmp_path)
    apply = lambda *args: None
    first = gateway.execute(config, request(), fake_gateway, apply)
    repeated = gateway.execute(config, request(), fake_gateway, apply)
    second = gateway.execute(config, request(target='target2', public=KEY2), fake_gateway, apply)
    assert first == repeated
    assert first['tunnel']['address'] != second['tunnel']['address']
    assert first['tunnel']['allowed_ips'] == '10.99.0.1/32'
    gateway.execute(config, request('remove'), fake_gateway, apply)
    with pytest.raises(ValueError, match='GATEWAY_PEER_REVOKED'):
        gateway.execute(config, request(), fake_gateway, apply)
    with pytest.raises(ValueError, match='GATEWAY_KEY_REUSED'):
        gateway.execute(config, request(target='other'), fake_gateway, apply)


def test_gateway_failure_keeps_reservation_for_reconciliation(tmp_path):
    config = gateway_config(tmp_path)
    def fail(*args):
        raise RuntimeError('network error')
    with pytest.raises(RuntimeError):
        gateway.execute(config, request(), fake_gateway, fail)
    before = json.loads(Path(config['ledger_path']).read_text())
    result = gateway.execute(config, request(), fake_gateway, lambda *a: None)
    assert result['tunnel']['address'] == before['target1:1']['address']


def test_gateway_stale_handshake_is_not_connected(tmp_path):
    config = gateway_config(tmp_path)
    def runner(args, **kwargs):
        return KEY if args[1] == 'pubkey' else KEY + '\t1'
    result = gateway.execute(config, request(), runner, lambda *a: None)
    assert result['reachable'] is False


def test_heartbeat_no_false_health_on_command_failure(local):
    config = configuration()
    config['kubeconfig'] = '/missing'
    def fail(*args, **kwargs):
        raise RuntimeError()
    assert client.checks(config, runner=fail, cli_factory=fail) == {'tunnel': False, 'openstack': False, 'runtime': False}


def test_gateway_apply_adds_peer_routes_and_blocks_transit(tmp_path, monkeypatch):
    config = gateway_config(tmp_path)
    peers = {'live': {'public_key': KEY2, 'address': '10.99.0.2/32', 'removed': False},
             'gone': {'public_key': KEY, 'address': '10.99.0.3/32', 'removed': True}}
    saved, commands = [], []
    monkeypatch.setattr(gateway, 'save', lambda path, text: saved.append(text))
    # Full native identity/readback behavior is tested in test_personal_gateway.py.
    monkeypatch.setattr(gateway, 'existing_interface', lambda *args: False)
    monkeypatch.setattr(gateway, 'ensure_forwarding', lambda *args: None)
    monkeypatch.setattr(gateway, 'verify_applied', lambda *args: None)
    def runner(args, **kwargs):
        commands.append(args)
        if args[:6] == ['ip', '-j', '-4', 'route', 'show', 'exact']:
            return '[]'
        if args[:4] == ['ip', '-4', 'route', 'show']:
            return '10.99.0.3 dev railshotwg'
        return ''
    gateway.apply(config, peers, runner)
    assert ['ip', '-4', 'route', 'replace', '10.99.0.2/32', 'dev', 'railshotwg'] in commands
    assert ['ip', '-4', 'route', 'del', '10.99.0.3/32', 'dev', 'railshotwg'] in commands
    assert 'PostUp = iptables' in saved[0]
    assert 'AllowedIPs = 10.99.0.2/32' in saved[0]
    assert 'AllowedIPs = 10.99.0.3/32' not in saved[0]


def test_metrics_measure_only_local_samples_and_never_invent_missing(tmp_path):
    from types import SimpleNamespace
    (tmp_path / 'net').mkdir()
    (tmp_path / 'stat').write_text('cpu 10 0 10 80 0 0 0 0\n')
    (tmp_path / 'meminfo').write_text('MemTotal: 100 kB\nMemAvailable: 40 kB\n')
    (tmp_path / 'net/dev').write_text('header\nheader\neth0: 1000 0 0 0 0 0 0 0 500 0 0 0 0 0 0 0\n')
    timestamps = iter([1, 11, 21])
    metrics = client.HostMetrics(tmp_path, clock=lambda: next(timestamps), disk=lambda _: SimpleNamespace(total=100, used=25))
    first = metrics.collect()
    assert first['cpu_percent'] is None and first['network_receive_bytes_per_second'] is None
    assert first['memory_percent'] == 60 and first['disk_percent'] == 25
    (tmp_path / 'stat').write_text('cpu 20 0 20 160 0 0 0 0\n')
    (tmp_path / 'net/dev').write_text('header\nheader\neth0: 2000 0 0 0 0 0 0 0 1000 0 0 0 0 0 0 0\n')
    second = metrics.collect()
    assert second['cpu_percent'] == 20 and second['network_receive_bytes_per_second'] == 100
    (tmp_path / 'meminfo').unlink()
    assert metrics.collect()['memory_percent'] is None


def test_release_package_is_deterministic_and_contains_no_tests_or_keys(tmp_path):
    import tarfile
    packaging = module('personal_test_packaging', 'deployment/scripts/package-personal-client.py')
    first = packaging.package(ROOT, tmp_path / 'one.tgz')
    second = packaging.package(ROOT, tmp_path / 'two.tgz')
    assert first['artifact_sha256'] == second['artifact_sha256']
    with tarfile.open(first['artifact_path']) as archive:
        names = archive.getnames()
        assert 'apps/agent/personal_remove.py' in names and 'apps/agent/openstack_control.py' in names
        assert 'deployment/bootstrap/install.sh' in names
        assert 'deployment/bootstrap/uninstall.sh' in names
        assert 'deployment/bootstrap/install_payload.py' in names
        assert 'deployment/bootstrap/client_setup/main.py' in names
        assert 'deployment/bootstrap/personal-install.sh' in names
        assert 'deployment/bootstrap/personal-registration.sh' in names
        assert 'deployment/bootstrap/revoke_personal_identity.py' in names
        assert 'deployment/bootstrap/client_setup/personal_identity.py' in names
        assert 'deployment/cloudflared/render.py' in names
        assert not any('tests/' in name or name.endswith('.key') for name in names)
    with pytest.raises(FileExistsError):
        packaging.package(ROOT, tmp_path / 'one.tgz')

    extracted = tmp_path / 'extracted'
    with tarfile.open(first['artifact_path']) as archive:
        archive.extractall(extracted, filter='data')
    legacy = subprocess.run(['bash', str(extracted / 'deployment/bootstrap/install.sh'), '--help'],
                            capture_output=True, text=True, check=False)
    assert legacy.returncode == 0, legacy.stderr
    assert '{init,diagnose,uninstall,verify-vm}' in legacy.stdout
    registration = (extracted / 'deployment/bootstrap/personal-registration.sh').read_text()
    assert "'deployment/cloudflared/render.py'" in registration
    spec = importlib.util.spec_from_file_location('packaged_runtime_access', extracted / 'apps/agent/runtime_access.py')
    packaged_access = importlib.util.module_from_spec(spec); spec.loader.exec_module(packaged_access)
    rendered = packaged_access.tunnel_renderer().render(namespace='personal-' + '0' * 36,
        name='railshot-tunnel', tunnel_id='11111111-1111-4111-8111-111111111111',
        credentials_secret='railshot-tunnel-credentials', hostnames=[], origin_vip='10.0.0.1',
        ca_configmap='railshot-tunnel-ca')
    assert [item['kind'] for item in rendered['items']] == ['ServiceAccount', 'ConfigMap', 'Deployment']


def test_cli_access_denies_root_shell_and_uses_dedicated_account(local, monkeypatch):
    from types import SimpleNamespace
    blob = base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + bytes(range(32))).decode()
    config = configuration()
    config['runtime_access'] = {'public_key': 'ssh-ed25519 ' + blob}
    home = local / 'unprivileged-cli'
    home.mkdir(mode=0o700)
    monkeypatch.setattr(client, 'CLI_HOME', home)
    monkeypatch.setattr(client, 'install_cli_account', lambda _: None)
    monkeypatch.setattr(client.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid()))
    commands = []
    monkeypatch.setattr(client, 'run', lambda args: commands.append(args) or '')
    client.install_runtime_access(config)
    config_text = (client.CONFIG / 'runtime_sshd_config').read_text()
    assert 'PermitRootLogin no' in config_text
    assert 'AllowUsers railshot-openstack' in config_text
    assert 'ForceCommand ' in config_text and 'openstack_control.py' in config_text
    assert 'k3s' not in config_text and 'PermitTTY no' in config_text
    assert str(home / 'authorized_keys') in config_text
    assert 'restrict,from="10.99.0.1/32"' in (home / 'authorized_keys').read_text()


def test_install_success_waits_for_server_control_verification():
    config = {**configuration(), 'project_id': 'project1'}
    calls = []
    statuses = iter(['connecting', 'ready'])
    client.wait_control_ready(config, transport=lambda *a, **kw: calls.append(a) or {'status': next(statuses)},
                              checker=lambda _: {'tunnel': True, 'openstack': True, 'runtime': True}, sleeper=lambda _: None)
    assert len(calls) == 2
    with pytest.raises(ValueError, match='CONTROL_CONNECTION_FAILED'):
        client.wait_control_ready(config, transport=lambda *a, **kw: {'status': 'attention'}, checker=lambda _: {}, sleeper=lambda _: None)


def test_enroll_uses_identity_bootstrap_without_application_credential_prompt(local, monkeypatch):
    from types import SimpleNamespace
    from client_setup import personal_identity
    auth = {'auth_url': 'https://keystone.example/v3', 'application_credential_id': 'generated',
            'application_credential_secret': 'secret'}
    identity_calls = []
    monkeypatch.setattr(personal_identity, 'prepare_personal_identity',
                        lambda *args, **kwargs: identity_calls.append((args, kwargs)) or
                        {'auth': auth, 'project_id': 'project1', 'ownership': {'version': 1}})
    original_require = client.require
    monkeypatch.setattr(client, 'require', lambda condition, code='CLIENT_INVALID':
                        None if code in ('ROOT_REQUIRED', 'CLIENT_INVALID') else original_require(condition, code))
    monkeypatch.setattr(client, 'OpenStackCLI', lambda value: SimpleNamespace(
        run=lambda argv: {'project_id': 'project1'} if argv == ['token', 'issue'] else []))
    def command(argv, **kwargs):
        if argv == ['wg', 'genkey']:
            return KEY
        if argv == ['wg', 'pubkey']:
            return KEY2
        if argv[0] == 'ssh-keygen':
            path = Path(argv[-1]); path.write_text('private'); path.chmod(0o600)
            path.with_suffix('.pub').write_text('ssh-ed25519 public comment')
            return ''
        raise AssertionError(argv)
    monkeypatch.setattr(client, 'run', command)
    response = {**configuration(), 'target_id': 'target1', 'generation': 1,
                'client_token': 'client-secret'}
    requests = []
    monkeypatch.setattr(client, 'api', lambda *args, **kwargs: requests.append((args, kwargs)) or response)
    monkeypatch.setattr(client, 'install_tunnel', lambda *args: None)
    monkeypatch.setattr(client, 'install_runtime_access', lambda *args: None)
    monkeypatch.setattr(client, 'install_service', lambda: None)
    monkeypatch.setattr(client, 'wait_control_ready', lambda *args: None)
    monkeypatch.setattr(client, 'prepare_runtime', lambda *args: None)
    monkeypatch.setattr(builtins, 'input', lambda *_: pytest.fail('Application credential input must not be used'))
    monkeypatch.setenv('RAILSHOT_ENROLLMENT_TOKEN', 'one-time-token')
    args = SimpleNamespace(api_url='https://example.test', enrollment_id='enrollment1', profile_id=None,
                           project_id=None, test_allow_http=False, runtime_config=None)
    client.enroll(args)
    assert identity_calls[0][0] == (client.CONFIG, client.STATE)
    assert identity_calls[0][1] == {'project_id': None, 'test_allow_http': False}
    assert requests[0][0][3]['project_id'] == 'project1'


def prepare_cli_owned(monkeypatch):
    from types import SimpleNamespace
    config = prepare_owned()
    home = remove.CLI_HOME
    home.mkdir(mode=0o700)
    (home / 'control.json').write_text('{}')
    account = SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid(), pw_dir=str(home))
    monkeypatch.setattr(remove.pwd, 'getpwnam', lambda _: account)
    (remove.CONFIG / 'cli-account.json').write_text(json.dumps({'user': remove.CLI_USER, 'uid': account.pw_uid, 'gid': account.pw_gid}))
    return config


def test_removal_cleans_only_recorded_cli_account(local, monkeypatch):
    config = prepare_cli_owned(monkeypatch)
    home = remove.CLI_HOME
    entries = home / '.cache/python-entrypoints'
    entries.mkdir(parents=True)
    (entries / ('a' * 64)).write_text('cached entry points')
    customer = local / 'customer-cache'
    customer.mkdir()
    (customer / 'keep').write_text('customer data')
    commands = []
    def runner(args):
        commands.append(args)
        if args[0] == 'userdel':
            monkeypatch.setattr(remove.pwd, 'getpwnam', lambda _: (_ for _ in ()).throw(KeyError()))
        return 'inactive' if args[0] == 'systemctl' and 'show' in args else ''
    remove.cleanup(config, runner=runner)
    assert ['userdel', 'railshot-openstack'] in commands
    assert not home.exists()
    assert (customer / 'keep').read_text() == 'customer data'


def test_removal_revokes_only_manifest_owned_identity_before_local_files(local):
    config = prepare_owned()
    (remove.STATE / 'identity-ownership.json').write_text(json.dumps({'version': 1, 'resources': []}))
    owner = remove.CONFIG / 'identity-owner'; owner.mkdir(mode=0o700)
    (owner / 'credentials.key').write_text('encrypted-key')
    (owner / 'credentials.enc').write_text('encrypted-owner')
    events = []
    def revoke(saved):
        assert saved == config and remove.CONFIG.exists() and remove.STATE.exists()
        events.append('revoke')
        return {'application_credential': 'revoked',
                'retained': ['service_user', 'role_assignment', 'shared_project']}
    def runner(argv):
        events.append(argv)
        return 'inactive' if argv[0] == 'systemctl' and 'show' in argv else ''
    steps = remove.cleanup(config, runner=runner, identity_revoker=revoke)
    assert events[0] == 'revoke'
    assert steps[0]['name'] == 'openstack-application-credential'
    assert not remove.CONFIG.exists() and not remove.STATE.exists()


def test_identity_revocation_failure_preserves_client_and_starts_no_local_mutation(local):
    config = prepare_owned()
    (remove.STATE / 'identity-ownership.json').write_text(json.dumps({'version': 1, 'resources': []}))
    commands = []
    with pytest.raises(RuntimeError, match='revocation failed'):
        remove.cleanup(config, runner=lambda argv: commands.append(argv),
                       identity_revoker=lambda _: (_ for _ in ()).throw(RuntimeError('revocation failed')))
    assert commands == [] and remove.CONFIG.exists() and remove.STATE.exists() and remove.PAYLOAD.exists()


def test_identity_revoker_uses_payload_venv_without_secret_arguments(local, monkeypatch):
    from types import SimpleNamespace
    interpreter = remove.PAYLOAD / '.venv/bin/python'; interpreter.parent.mkdir(parents=True)
    interpreter.write_text('verified interpreter')
    entrypoint = remove.PAYLOAD / 'deployment/bootstrap/revoke_personal_identity.py'
    entrypoint.parent.mkdir(parents=True); entrypoint.write_text('verified entrypoint')
    calls = []
    monkeypatch.setattr(remove.subprocess, 'run', lambda argv, **kwargs:
                        calls.append((argv, kwargs)) or SimpleNamespace(returncode=0,
                        stdout='{"application_credential":"revoked","retained":["shared_project"]}'))
    result = remove.revoke_identity({**configuration(), 'test_allow_http': True})
    argv, options = calls[0]
    assert argv == [str(interpreter), str(entrypoint), '--config-dir', str(remove.CONFIG),
                    '--state-dir', str(remove.STATE), '--test-allow-http']
    assert options['env'] == {'PATH': '/usr/bin:/bin',
                              'PYTHONPATH': str(remove.PAYLOAD / 'deployment/bootstrap') + ':' + str(remove.PAYLOAD),
                              'PYTHONDONTWRITEBYTECODE': '1'}
    assert 'secret' not in json.dumps([argv, options['env']])
    assert result['application_credential'] == 'revoked'


def test_identity_revoker_nonzero_exit_is_not_accepted(local, monkeypatch):
    from types import SimpleNamespace
    interpreter = remove.PAYLOAD / '.venv/bin/python'; interpreter.parent.mkdir(parents=True)
    interpreter.write_text('verified interpreter')
    entrypoint = remove.PAYLOAD / 'deployment/bootstrap/revoke_personal_identity.py'
    entrypoint.parent.mkdir(parents=True); entrypoint.write_text('verified entrypoint')
    monkeypatch.setattr(remove.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout=''))
    with pytest.raises(ValueError, match='REMOVAL_OWNERSHIP_UNVERIFIED'):
        remove.revoke_identity(configuration())


@pytest.mark.parametrize('foreign', ['directory', 'symlink', 'file_symlink', 'hardlink', 'writable', 'unexpected_name'])
def test_removal_rejects_unowned_cache_before_any_effect(local, monkeypatch, foreign):
    config = prepare_cli_owned(monkeypatch)
    entries = remove.CLI_HOME / '.cache/python-entrypoints'
    entries.mkdir(parents=True)
    customer = local / 'customer-file'
    customer.write_text('keep')
    cached = entries / ('a' * 64)
    if foreign == 'directory':
        (entries.parent / 'customer-data').mkdir()
    elif foreign == 'symlink':
        entries.rmdir()
        entries.symlink_to(local, target_is_directory=True)
    elif foreign == 'file_symlink':
        cached.symlink_to(customer)
    elif foreign == 'hardlink':
        os.link(customer, cached)
    elif foreign == 'writable':
        cached.write_text('cache')
        cached.chmod(0o666)
    else:
        (entries / 'customer-data').write_text('keep')
    commands = []
    with pytest.raises(remove.RemovalPreflightError):
        remove.cleanup(config, runner=lambda args: commands.append(args))
    assert commands == []
    assert remove.UNIT.exists() and remove.WG.exists() and remove.CONFIG.exists()
    assert customer.read_text() == 'keep'


def test_cli_cache_rejects_different_owner(local, monkeypatch):
    prepare_cli_owned(monkeypatch)
    (remove.CLI_HOME / '.cache').mkdir()
    with pytest.raises(ValueError):
        remove.inspect_cli_cache(os.geteuid() + 1)


def test_only_owned_cli_account_deletion_has_longer_deadline(monkeypatch):
    from types import SimpleNamespace
    calls = []
    def execute(args, **kwargs):
        calls.append((args, kwargs['timeout']))
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(remove.subprocess, 'run', execute)
    remove.run(['userdel', remove.CLI_USER])
    remove.run(['systemctl', 'disable', '--now', 'railshot-personal'])
    remove.run(['wg', 'show', 'interfaces'])
    assert [deadline for _, deadline in calls] == [180, 45, 45]


@pytest.mark.parametrize('base', ['http://example.test', 'http://127.0.0.1:8080', 'http://10.26.2.180'])
def test_http_requires_explicit_boolean_opt_in(base):
    for option in (False, None, '1', 1):
        with pytest.raises(ValueError, match='HTTPS_REQUIRED'):
            client.https_url(base, test_allow_http=option)
    assert client.https_url(base, test_allow_http=True) == base


@pytest.mark.parametrize('base', ['http://user:pass@example.test', 'http://example.test/?token=x',
                                  'http://example.test/#x', 'ftp://example.test', 'http://example.test/a b'])
def test_test_mode_preserves_url_restrictions(base):
    with pytest.raises(ValueError):
        client.https_url(base, test_allow_http=True)


def test_http_option_survives_saved_config_and_daemon_restart(local, monkeypatch):
    config = {**configuration(), 'project_id': 'project1', 'api_url': 'http://127.0.0.1:8080', 'test_allow_http': True}
    client.save_config(config)
    calls = []
    monkeypatch.setattr(client, 'checks', lambda _: {})
    monkeypatch.setattr(client, 'api', lambda *args, **kwargs: calls.append((args, kwargs)) or {})
    def stop(_):
        raise RuntimeError('stop daemon')
    monkeypatch.setattr(client.time, 'sleep', stop)
    with pytest.raises(RuntimeError, match='stop daemon'):
        client.daemon()
    assert calls[0][0][0] == config['api_url']
    assert calls[0][1] == {'test_allow_http': True}
    client.receipt(config, {'status': 'running'})
    assert calls[-1][1] == {'test_allow_http': True}
    client.wait_control_ready(config, transport=lambda *args, **kwargs: calls.append((args, kwargs)) or {'status': 'ready'}, checker=lambda _: {})
    assert calls[-1][1] == {'test_allow_http': True}


def test_removal_receipt_http_requires_saved_test_option(monkeypatch):
    from types import SimpleNamespace
    calls = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def opened(request, **kwargs):
        calls.append(request)
        return Response()
    monkeypatch.setattr(remove.urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=opened))
    config = {**configuration(), 'api_url': 'http://example.test'}
    for value in (False, '1', 1):
        with pytest.raises(ValueError):
            remove.send({**config, 'test_allow_http': value}, {})
    assert calls == []
    remove.send({**config, 'test_allow_http': True}, {'status': 'succeeded'})
    assert calls[0].full_url == 'http://example.test/api/v1/targets/target1/receipts'


@pytest.mark.parametrize('version, allowed', [('24.04', True), ('26.04', True), ('22.04', False), ('26.10', False)])
@pytest.mark.parametrize('url, opt_in, allowed_url', [('https://example.test', '0', True), ('http://10.26.2.180', '0', False), ('http://10.26.2.180', '1', True), ('ftp://example.test', '1', False)])
def test_installer_operating_system_and_url_gate(version, allowed, url, opt_in, allowed_url, monkeypatch):
    script = (ROOT / 'deployment/bootstrap/personal-registration.sh').read_text()
    validation = script.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    monkeypatch.setattr(Path, 'read_text', lambda self: 'ID=ubuntu\nVERSION_ID="' + version + '"\n')
    monkeypatch.setattr(sys, 'argv', ['-', url, url + '/payload.tgz', '', opt_in])
    if allowed and allowed_url:
        exec(compile(validation, 'installer-validation', 'exec'), {})
    else:
        with pytest.raises(SystemExit):
            exec(compile(validation, 'installer-validation', 'exec'), {})


def test_canonical_installer_owns_personal_mode_and_legacy_wrapper_delegates():
    canonical = (ROOT / 'deployment/bootstrap/install.sh').read_text()
    compatibility = (ROOT / 'deployment/bootstrap/personal-install.sh').read_text()
    assert "${1:-} == '--personal-registration'" in canonical
    assert '/api/v1/readiness?scope=personal' in canonical
    assert 'personal_registration "$PERSONAL_ENROLLMENT_SECRET"' in canonical
    assert canonical.index('unset RAILSHOT_ENROLLMENT_TOKEN') < canonical.index('SCRIPT_DIR=')
    assert canonical.index('/api/v1/readiness?scope=personal') < canonical.index('curl --disable')
    assert "if ! python3 - \"$work_dir\" \"$artifact_sha256\"" in canonical
    assert "rm -rf -- \"$work_dir\"" in canonical
    assert 'install.sh" --personal-registration' in compatibility
    assert 'Application Credential ID' not in compatibility



def test_cli_account_commands_allow_slow_account_database_writes(local, monkeypatch):
    from types import SimpleNamespace
    home = local / 'account-home'
    monkeypatch.setattr(client, 'CLI_HOME', home)
    account = SimpleNamespace(pw_uid=994, pw_gid=980, pw_dir=str(home), pw_shell='/bin/sh')
    lookups = []
    def lookup(_):
        lookups.append(True)
        if len(lookups) == 1:
            raise KeyError(client.CLI_USER)
        return account
    monkeypatch.setattr(client.pwd, 'getpwnam', lookup)
    monkeypatch.setattr(client.os, 'chown', lambda *args: None)
    calls = []
    monkeypatch.setattr(client, 'run', lambda argv, **kwargs: calls.append((argv, kwargs)) or '')
    client.CredentialStore(client.CONFIG).save({'auth_url': 'https://example.test', 'application_credential_id': 'test'})
    config = {**configuration(), 'project_id': 'project1'}
    client.install_cli_account(config)
    assert [argv[0] for argv, kwargs in calls] == ['useradd', 'usermod']
    assert all(kwargs == {'timeout': 180} for argv, kwargs in calls)
    assert json.loads((client.CONFIG / 'cli-account.json').read_text()) == {'user': client.CLI_USER, 'uid': account.pw_uid, 'gid': account.pw_gid}
    assert json.loads((home / 'control.json').read_text())['target_id'] == config['target_id']


def test_existing_cli_account_without_marker_is_never_adopted(local, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(client.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=994, pw_gid=980, pw_dir=str(client.CLI_HOME), pw_shell='/bin/sh'))
    calls = []
    monkeypatch.setattr(client, 'run', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match='CLI_ACCOUNT_NOT_OWNED'):
        client.install_cli_account({**configuration(), 'project_id': 'project1'})
    assert calls == []
    assert not (client.CONFIG / 'cli-account.json').exists()


def test_initial_connection_wait_does_not_wait_for_unsubmitted_runtime():
    config = {**configuration(), 'project_id': 'project1'}
    calls = []
    client.wait_control_ready(config, transport=lambda *args, **kwargs: calls.append(args) or
        {'status': 'preparing', 'connection_status': 'ready'}, checker=lambda _: {}, sleeper=lambda _: pytest.fail('Must not wait for runtime yet'))
    assert len(calls) == 1


def test_all_in_one_waits_for_server_permission_verification(local, monkeypatch):
    from types import SimpleNamespace
    from apps.agent import personal_runtime, runtime_access
    config = {**configuration(), 'project_id': 'project1'}
    runtime = {'resource_id': 'server1', 'private_ipv4': '10.0.0.17', 'management_network': 'private',
               'placement': 'nova', 'architecture': 'amd64', 'initialization': 'preconfigured'}
    monkeypatch.setattr(client, 'CredentialStore', lambda _: SimpleNamespace(load=lambda: {}))
    monkeypatch.setattr(client, 'OpenStackCLI', lambda _: object())
    def prepared(*args, **kwargs):
        kwargs['progress']('client_selection')
        kwargs['progress']('client_verification')
        return runtime
    monkeypatch.setattr(personal_runtime, 'prepare', prepared)
    monkeypatch.setattr(runtime_access, 'install', lambda *args: {'ssh_user': 'railshot-runtime', 'ssh_port': 2223, 'ssh_host_key': 'public'})
    replies = iter([{'runtime_preparation': {'status': 'not_started', 'stage': 'client'}},
                    {'status': 'preparing', 'deployable': False, 'runtime_preparation': {'status': 'running'}},
                    {'status': 'ready', 'deployable': True, 'runtime_preparation': {'status': 'succeeded'}}])
    sent, sleeps = [], []
    def transport(*args, **kwargs):
        if kwargs.get('method') == 'GET': return next(replies)
        sent.append(args[3]); return {}
    monkeypatch.setattr(client, 'api', transport)
    monkeypatch.setattr(client.time, 'sleep', lambda seconds: sleeps.append(seconds))
    client.prepare_runtime(config, SimpleNamespace(runtime_config=None))
    assert sleeps == [5]
    assert [row['progress']['stage'] for row in sent if 'progress' in row] == ['client_selection', 'client_verification']
    assert sent[-1]['evidence'] == {**runtime, 'ssh_user': 'railshot-runtime', 'ssh_port': 2223, 'ssh_host_key': 'public'}


def test_uncertain_runtime_is_not_automatically_retried(local, monkeypatch):
    from types import SimpleNamespace
    from apps.agent import personal_runtime
    monkeypatch.setattr(client, 'api', lambda *args, **kwargs: {'runtime_preparation': {'status': 'unknown'}})
    monkeypatch.setattr(personal_runtime, 'prepare', lambda *args, **kwargs: pytest.fail('Unknown preparation must not replay'))
    with pytest.raises(ValueError, match='RUNTIME_PRIOR_OUTCOME_UNKNOWN'):
        client.prepare_runtime({**configuration(), 'project_id': 'project1'}, SimpleNamespace())


def test_removal_revokes_owned_runtime_relay_but_keeps_customer_vm_key(local, monkeypatch):
    from types import SimpleNamespace
    config = prepare_owned()
    home = remove.KUBE_HOME
    home.mkdir()
    (home / 'authorized_keys').write_text('owned public key')
    account = SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid(), pw_dir=str(home))
    monkeypatch.setattr(remove.pwd, 'getpwnam', lambda _: account)
    (remove.CONFIG / 'runtime-access-account.json').write_text(json.dumps({'user': remove.KUBE_USER, 'uid': account.pw_uid, 'gid': account.pw_gid}))
    original_key = local / 'customer-existing-vm-key'
    original_key.write_text('must remain')
    (remove.CONFIG / 'runtime.json').write_text(json.dumps({'ssh': {'identity_file': str(original_key)}}))
    state = remove.STATE / 'runtime-ansible'; state.mkdir()
    (state / 'receipt.json').write_text('{}')
    for path in (remove.KUBE_UNIT, remove.KUBE_PROXY_UNIT, remove.KUBE_SUDOERS):
        path.write_text(remove.MARKER + 'owned')
    remove.KUBE_WRAPPER.write_text('#!/bin/sh\n' + remove.MARKER + 'fixed wrapper')
    commands = []
    def runner(args):
        commands.append(args)
        if args[0] == 'userdel':
            monkeypatch.setattr(remove.pwd, 'getpwnam', lambda _: (_ for _ in ()).throw(KeyError()))
        return 'inactive' if args[0] == 'systemctl' and 'show' in args else ''
    remove.cleanup(config, runner=runner)
    assert ['userdel', remove.KUBE_USER] in commands
    assert original_key.read_text() == 'must remain'
    assert not home.exists() and not remove.KUBE_WRAPPER.exists() and not remove.KUBE_SUDOERS.exists()
    assert not any(call[0] in ('ssh', 'openstack', 'kubectl') for call in commands)


def test_running_removal_attempt_is_not_marked_unknown(local, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(client, 'removal_module', lambda: SimpleNamespace(helper_active=lambda *a: True))
    receipts, launches = [], []
    command = {'id': 'op1', 'operation_id': 'op1', 'attempt_id': 'attempt1', 'kind': 'environment.delete',
               'applications': [], 'generation': 1, 'delete_data': True}
    for _ in range(2):
        client.handle_command(configuration(), command, sender=lambda c, r: receipts.append(r), launch=lambda *a: launches.append(a))
    assert len(launches) == 1
    assert [r['status'] for r in receipts] == ['running', 'running']
    assert all(r['attempt_id'] == 'attempt1' for r in receipts)


def test_legacy_job_needs_fresh_intact_probe_and_explicit_attempt(local, monkeypatch):
    from types import SimpleNamespace
    state = {'state': 'intact', 'preflight_ok': True, 'helper_active': False}
    module = SimpleNamespace(helper_active=lambda *a: False, inspect_intact=lambda *a: copy_dict(state))
    monkeypatch.setattr(client, 'removal_module', lambda: module)
    receipts, launches = [], []
    old = {'id': 'op1', 'operation_id': 'op1', 'kind': 'environment.delete', 'applications': [], 'generation': 1, 'delete_data': True}
    config = configuration()
    client.handle_command(config, old, sender=lambda c, r: receipts.append(r), launch=lambda *a: launches.append(a))
    resumed = {**old, 'attempt_id': 'attempt2', 'reconciliation_id': 'probe1'}
    with pytest.raises(FileNotFoundError):
        client.handle_command(config, resumed, sender=lambda *a: None, launch=lambda *a: launches.append(a))
    inspection = {'id': 'probe1', 'kind': 'environment.inspect', 'operation_id': 'op1', 'reconciliation_id': 'probe1', 'generation': 1}
    client.handle_command(config, inspection, sender=lambda c, r: receipts.append(r))
    assert receipts[-1]['status'] == 'inspected' and receipts[-1]['inspection'] == state
    state['state'] = 'partial'
    with pytest.raises(ValueError, match='REMOVAL_STATE_CHANGED'):
        client.handle_command(config, resumed, sender=lambda *a: None, launch=lambda *a: launches.append(a))
    state['state'] = 'intact'
    client.handle_command(config, resumed, sender=lambda c, r: receipts.append(r), launch=lambda *a: launches.append(a))
    assert len(launches) == 2 and launches[-1][0]['attempt_id'] == 'attempt2'
    # The original attempt cannot be replayed after a newer attempt was admitted.
    with pytest.raises(ValueError, match='REMOVAL_RECONCILIATION_REQUIRED'):
        client.handle_command(config, old, sender=lambda *a: None)


def copy_dict(value):
    return json.loads(json.dumps(value))


def test_intact_inspection_is_readonly_and_requires_every_service(local):
    config = prepare_owned()
    venv = remove.PAYLOAD / '.venv'; (venv / 'bin').mkdir(parents=True)
    (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
    (venv / 'bin/python').write_text('synthetic interpreter')
    calls = []
    def runner(args):
        calls.append(args)
        if 'list-units' in args:
            return ''
        if args[0] == 'wg':
            return 'wg0 railshot0'
        return 'active'
    assert remove.inspect_intact(config, 'operation1', runner=runner) == {'state': 'intact', 'preflight_ok': True, 'helper_active': False}
    assert all(args[0] == 'wg' or args[1] in ('show', 'list-units') for args in calls)
    leftover = local / 'railshot-remove-operation1.json'
    leftover.write_text('staged removal')
    assert remove.inspect_intact(config, 'operation1', runner=runner)['state'] == 'partial'
    leftover.unlink()
    remove.UNIT.unlink()
    assert remove.inspect_intact(config, 'operation1', runner=runner)['state'] == 'partial'


@pytest.mark.parametrize('damage', ['foreign_file', 'modified_source', 'symlink', 'unknown_cache'])
def test_payload_manifest_blocks_unsafe_recursive_removal(local, damage):
    import hashlib
    config = prepare_owned()
    source = remove.PAYLOAD / 'apps/agent/owned.py'; source.parent.mkdir(parents=True)
    source.write_text('print("owned")')
    marker = remove.PAYLOAD / '.railshot-personal.json'
    marker.write_text(json.dumps({'version': 1, 'files': {'apps/agent/owned.py': hashlib.sha256(source.read_bytes()).hexdigest()}}))
    if damage == 'foreign_file':
        (remove.PAYLOAD / 'customer-data').write_text('keep')
    elif damage == 'modified_source':
        source.write_text('changed')
    elif damage == 'symlink':
        (remove.PAYLOAD / 'other').symlink_to(local)
    else:
        (source.parent / '__pycache__').mkdir()
        (source.parent / '__pycache__/customer.cpython-314.pyc').write_bytes(b'keep')
    commands = []
    with pytest.raises(remove.RemovalPreflightError):
        remove.cleanup(config, runner=lambda args: commands.append(args))
    assert commands == [] and remove.PAYLOAD.exists() and remove.UNIT.exists()


def test_legacy_payload_manifest_accepts_its_python_cache_and_isolated_venv(local):
    import hashlib
    prepare_owned()
    source = remove.PAYLOAD / 'apps/agent/owned.py'; source.parent.mkdir(parents=True)
    source.write_text('print("owned")')
    (remove.PAYLOAD / '.railshot-personal.json').write_text(json.dumps({'version': 1, 'files': {
        'apps/agent/owned.py': hashlib.sha256(source.read_bytes()).hexdigest()}}))
    cache = source.parent / '__pycache__'; cache.mkdir()
    (cache / 'owned.cpython-314.pyc').write_bytes(b'generated')
    venv = remove.PAYLOAD / '.venv'; (venv / 'bin').mkdir(parents=True)
    (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
    (venv / 'bin/python').symlink_to('/usr/bin/python3')
    remove.inspect_payload()


def test_main_attests_success_only_after_self_and_every_owned_path_are_absent(local, monkeypatch):
    config = prepare_owned(); config.update(operation_id='op1', attempt_id='attempt1')
    # Saved client.json intentionally excludes ephemeral removal fields.
    job = remove.STATE / 'job-op1.json'; job.write_text(json.dumps({'fingerprint': 'synthetic', 'attempt_id': 'attempt1'}))
    script = local / 'temporary-helper.py'; script.write_text('owned helper')
    secret = local / 'temporary-helper.json'; secret.write_text(json.dumps(config)); secret.chmod(0o600)
    monkeypatch.setattr(remove, '__file__', str(script))
    monkeypatch.setattr(remove.sys, 'argv', [str(script), str(secret)])
    original = remove.cleanup
    calls = []
    def runner(args):
        calls.append(args)
        return 'inactive' if args[0] == 'systemctl' and 'show' in args else 'wg0' if args[0] == 'wg' else ''
    monkeypatch.setattr(remove, 'cleanup', lambda c, **kwargs: original(c, runner=runner, **kwargs))
    receipts = []
    def sender(c, result):
        assert not any(p.exists() for p in (script, secret, remove.PAYLOAD, remove.CONFIG, remove.STATE, remove.UNIT, remove.WG))
        receipts.append(copy_dict(result))
    monkeypatch.setattr(remove, 'send_with_retries', sender)
    assert remove.main() == 0
    assert receipts[0]['status'] == 'succeeded' and receipts[0]['client_removed'] is True and receipts[0]['attempt_id'] == 'attempt1'
    assert not any('wg0' in arg for args in calls for arg in args)


def test_preflight_failure_receipt_is_durable_and_has_no_destructive_effect(local, monkeypatch):
    config = prepare_owned(); config.update(operation_id='op1', attempt_id='attempt1')
    (remove.PAYLOAD / 'customer-file').write_text('keep')
    job = remove.STATE / 'job-op1.json'; job.write_text(json.dumps({'fingerprint': 'synthetic', 'attempt_id': 'attempt1'}))
    script = local / 'temporary-helper.py'; script.write_text('owned helper')
    secret = local / 'temporary-helper.json'; secret.write_text(json.dumps(config)); secret.chmod(0o600)
    monkeypatch.setattr(remove, '__file__', str(script)); monkeypatch.setattr(remove.sys, 'argv', [str(script), str(secret)])
    receipts = []
    monkeypatch.setattr(remove, 'send_with_retries', lambda c, result: receipts.append(copy_dict(result)))
    assert remove.main() == 1
    assert receipts[0]['status'] == 'blocked' and receipts[0]['mutation_started'] is False
    assert json.loads(job.read_text())['receipt'] == receipts[0]
    assert remove.UNIT.exists() and remove.WG.exists() and (remove.PAYLOAD / 'customer-file').read_text() == 'keep'


def test_lost_receipt_retries_identical_evidence_without_cleanup():
    calls = []
    result = {'status': 'succeeded', 'attempt_id': 'attempt1'}
    def sender(config, receipt):
        calls.append(copy_dict(receipt))
        if len(calls) < 3:
            raise TimeoutError()
    remove.send_with_retries({}, result, sender=sender, sleeper=lambda _: None)
    assert calls == [result, result, result]


def test_late_helper_cannot_overwrite_a_new_attempt_job(local, monkeypatch):
    config = prepare_owned(); config.update(operation_id='op1', attempt_id='old-attempt')
    job = remove.STATE / 'job-op1.json'
    saved = {'fingerprint': 'synthetic', 'attempt_id': 'new-attempt', 'status': 'removing'}
    job.write_text(json.dumps(saved))
    script = local / 'old-helper.py'; script.write_text('owned helper')
    secret = local / 'old-helper.json'; secret.write_text(json.dumps(config)); secret.chmod(0o600)
    monkeypatch.setattr(remove, '__file__', str(script)); monkeypatch.setattr(remove.sys, 'argv', [str(script), str(secret)])
    original = remove.cleanup
    calls = []
    monkeypatch.setattr(remove, 'cleanup', lambda c, **kwargs: original(c, runner=lambda args: calls.append(args), **kwargs))
    receipts = []
    monkeypatch.setattr(remove, 'send_with_retries', lambda c, result: receipts.append(copy_dict(result)))
    assert remove.main() == 1
    assert json.loads(job.read_text()) == saved
    assert calls == [] and remove.UNIT.exists() and remove.PAYLOAD.exists()
    assert receipts[0]['attempt_id'] == 'old-attempt' and receipts[0]['status'] == 'unknown'


def test_runtime_retry_reconciles_saved_evidence_before_resuming_without_reinstall(local, monkeypatch):
    from types import SimpleNamespace
    from apps.agent import personal_runtime
    evidence = {'resource_id': 'server1', 'private_ipv4': '10.0.0.17'}
    (client.CONFIG / 'runtime-evidence.json').write_text(json.dumps(evidence))
    (client.CONFIG / 'runtime-evidence.json').chmod(0o600)
    states = iter([
        {'runtime_preparation': {'status': 'unknown', 'stage': 'reconciliation'}},
        {'runtime_preparation': {'status': 'blocked', 'stage': 'registration', 'resumable': True}},
        {'status': 'ready', 'deployable': True, 'runtime_preparation': {'status': 'succeeded'}}])
    sent = []
    def transport(*args, **kwargs):
        if kwargs.get('method') == 'GET':
            return next(states)
        sent.append(args[3]); return {}
    monkeypatch.setattr(client, 'api', transport)
    monkeypatch.setattr(personal_runtime, 'prepare', lambda *a, **kw: pytest.fail('Saved evidence must not reinstall K3s'))
    client.prepare_runtime({**configuration(), 'project_id': 'project1'}, SimpleNamespace())
    assert sent == [{'generation': 1, 'action': 'reconcile'}, {'generation': 1, 'action': 'resume', 'evidence': evidence}]
