import base64
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('openstack_control_test', ROOT / 'apps/agent/openstack_control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
CONFIG = {'project_id': 'project1', 'target_id': 'personal-one', 'generation': 1}
AUTH = {'auth_url': 'https://keystone.example/v3', 'application_credential_id': 'credential-id',
        'application_credential_secret': 'highly-secret-value'}


def request(argv, job='job1', **params):
    return {'version': 1, 'job_id': job, 'action': 'openstack.execute', 'params': {'argv': argv, **params}}


@pytest.mark.parametrize('argv', [
    ['server', 'list', '--all-projects'], ['server', 'list', '--os-token', 'secret'],
    ['server', 'show', '--help'], ['server', 'create', '--user-data', '/etc/shadow', 'app'],
    ['server', 'create', '--flavor=small', 'app'], ['server', 'create', '--file', '/etc/shadow', 'app'],
    ['keypair', 'create', '--private-key', '/tmp/leak', 'key'], ['token', 'issue'],
    ['console', 'log', 'show', 'server1'], ['project', 'delete', 'project1'],
    ['role', 'add', 'admin'], ['server', 'list', ';', 'sh'], ['server', 'list', '--debug'],
    ['server', 'list', '--insecure'], ['server', 'create', '--project', 'other', 'app'],
    ['server', 'list', '--format', 'shell'], ['server', 'delete', '--all'],
])
def test_rejects_dangerous_or_unscoped_cli(argv):
    with pytest.raises(control.ProtocolError):
        control.decode(json.dumps(request(argv)).encode())


def test_strict_json_and_original_command(tmp_path):
    with pytest.raises(control.ProtocolError):
        control.decode(b'{"version":1,"version":1}')
    with pytest.raises(control.ProtocolError):
        control.decode(b'{"version":NaN}')
    loaded = []
    result = control.execute_raw(json.dumps(request(['server', 'list'])).encode(), 'sh -c id',
                                 lambda: loaded.append(True), CONFIG, home=tmp_path)
    assert not result['ok'] and not loaded


class FakeCloud:
    def __init__(self):
        self.calls = []
        self.resource = None
        self.fail_create = False
        self.project = 'project1'

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv == ['token', 'issue']:
            return {'id': 'secret-issued-token', 'project_id': self.project}
        if argv == ['server', 'list']:
            return [{'ID': 'server1', 'Name': 'web', 'Status': 'ACTIVE', 'private_key': 'omit'}]
        if argv[:2] == ['server', 'create']:
            if self.fail_create:
                raise control.ProviderError('timeout')
            self.resource = {'id': 'server1', 'name': 'web', 'project_id': self.project, 'adminPass': 'omit'}
            return self.resource
        if argv[:2] == ['server', 'show']:
            if self.resource is None:
                raise control.ProviderError('not_found')
            return self.resource
        if argv[:2] == ['server', 'delete']:
            assert kwargs == {'json_output': False}
            self.resource = None
            return None
        raise AssertionError(argv)


def test_read_filters_credentials_and_project_scope(tmp_path):
    cli = FakeCloud()
    result = control.execute(request(['server', 'list']), CONFIG, cli, tmp_path)
    assert result == [{'ID': 'server1', 'Name': 'web', 'Status': 'ACTIVE'}]
    assert not list(tmp_path.iterdir())
    cli.project = 'other'
    with pytest.raises(control.ProtocolError):
        control.execute(request(['server', 'list']), CONFIG, cli, tmp_path)
    assert cli.calls[-1] == ['token', 'issue']


def test_create_delete_only_owned_and_explicit_data_confirmation(tmp_path):
    tmp_path.chmod(0o700)
    cli = FakeCloud()
    create = request(['server', 'create', '--image', 'image1', '--flavor', 'small', 'web'])
    first = control.execute(create, CONFIG, cli, tmp_path)
    second = control.execute(create, CONFIG, cli, tmp_path)
    assert first == second and 'adminPass' not in first
    assert len([c for c in cli.calls if c[:2] == ['server', 'create']]) == 1
    with pytest.raises(control.ProtocolError):
        control.execute(request(['server', 'delete', 'customer-server'], 'delete-other', delete_data=True), CONFIG, cli, tmp_path)
    with pytest.raises(control.ProtocolError):
        control.execute(request(['server', 'delete', 'server1'], 'delete-no-confirm'), CONFIG, cli, tmp_path)
    deleted = control.execute(request(['server', 'delete', 'server1'], 'delete1', delete_data=True), CONFIG, cli, tmp_path)
    assert deleted == {'id': 'server1', 'status': 'deleted'}
    assert json.loads((tmp_path / 'resources.json').read_text())['server:server1']['removed'] is True


def test_mutation_timeout_is_unknown_and_never_replayed(tmp_path):
    tmp_path.chmod(0o700)
    cli = FakeCloud()
    cli.fail_create = True
    payload = json.dumps(request(['server', 'create', '--image', 'image1', '--flavor', 'small', 'web'])).encode()
    first = control.execute_raw(payload, control.COMMAND, lambda: AUTH, CONFIG, cli_factory=lambda _: cli, home=tmp_path)
    second = control.execute_raw(payload, control.COMMAND, lambda: AUTH, CONFIG, cli_factory=lambda _: cli, home=tmp_path)
    assert first['error']['code'] == second['error']['code'] == 'execution_unknown'
    assert len([c for c in cli.calls if c[:2] == ['server', 'create']]) == 1


def test_job_id_rebinding_does_not_create_other_resource(tmp_path):
    tmp_path.chmod(0o700)
    cli = FakeCloud()
    control.execute(request(['server', 'create', 'web']), CONFIG, cli, tmp_path)
    with pytest.raises(control.ProtocolError):
        control.execute(request(['server', 'create', 'other']), CONFIG, cli, tmp_path)
    assert len([c for c in cli.calls if c[:2] == ['server', 'create']]) == 1


def test_real_openstack_cli_adapter_keeps_credentials_off_argv(tmp_path):
    tmp_path.chmod(0o700)
    control.CredentialStore(tmp_path).save(AUTH)
    invoked = []
    def cloud_process(argv, **kwargs):
        invoked.append(argv)
        assert argv[0] == 'openstack'
        assert AUTH['application_credential_secret'] not in json.dumps(argv)
        file = Path(kwargs['env']['OS_CLIENT_CONFIG_FILE'])
        assert file.stat().st_mode & 0o077 == 0
        credentials = json.loads(file.read_text())['clouds']['railshot']['auth']
        assert credentials == AUTH
        assert kwargs['shell'] is False
        body = {'project_id': 'project1', 'id': 'secret-issued-token'} if argv[3:5] == ['token', 'issue'] else [{'ID': 'server1', 'Name': 'web', 'Status': 'ACTIVE'}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr='')
    response = control.execute_raw(json.dumps(request(['server', 'list'])).encode(), control.COMMAND,
                                  lambda: control.CredentialStore(tmp_path).load(), CONFIG,
                                  cli_factory=lambda auth: control.OpenStackCLI(auth, runner=cloud_process), home=tmp_path)
    assert response['ok'] and response['result'][0]['ID'] == 'server1'
    assert [args[3:5] for args in invoked] == [['token', 'issue'], ['server', 'list']]
    assert 'secret-issued-token' not in json.dumps(response)


def test_response_checks_known_secrets_even_when_json_escapes_them():
    with pytest.raises(control.ProtocolError):
        control.sanitize({'name': 'value secret\nvalue'}, ('secret\nvalue',))


def test_same_name_keypair_replacement_is_not_deleted(tmp_path):
    tmp_path.chmod(0o700)
    blob = base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + bytes(range(32))).decode()
    public = 'ssh-ed25519 ' + blob
    class KeyCloud:
        def run(self, argv, **kwargs):
            if argv == ['token', 'issue']:
                return {'id': 'token', 'project_id': 'project1'}
            if argv[:2] == ['keypair', 'create']:
                return {'name': 'key1', 'fingerprint': 'fingerprint1', 'public_key': public}
            if argv[:2] == ['keypair', 'show']:
                return {'name': 'key1', 'fingerprint': 'replacement', 'public_key': public}
            raise AssertionError('Must not delete replacement key')
    cloud = KeyCloud()
    control.execute(request(['keypair', 'create', 'key1'], public_key=public), CONFIG, cloud, tmp_path)
    with pytest.raises(control.ProtocolError):
        control.execute(request(['keypair', 'delete', 'key1'], job='delete'), CONFIG, cloud, tmp_path)


def test_neutron_lists_are_forced_to_enrolled_project(tmp_path):
    calls = []
    class Cli:
        def run(self, argv):
            calls.append(argv)
            return {'project_id': 'project1'} if argv[:2] == ['token', 'issue'] else []
    assert control.execute(request(['network', 'list']), CONFIG, Cli(), tmp_path) == []
    assert calls[-1] == ['network', 'list', '--project', 'project1']


def test_cross_project_security_group_write_is_rejected_before_intent(tmp_path):
    tmp_path.chmod(0o700)
    class Cli:
        def run(self, argv):
            if argv[:2] == ['token', 'issue']:
                return {'project_id': 'project1'}
            assert argv == ['security', 'group', 'show', 'foreign-group']
            return {'id': 'foreign-group', 'project_id': 'other'}
    with pytest.raises(control.ProtocolError):
        control.execute(request(['security', 'group', 'rule', 'create', '--ingress', '--protocol', 'tcp', 'foreign-group']), CONFIG, Cli(), tmp_path)
    assert not (tmp_path / 'job-job1.json').exists()


def test_cross_project_floating_ip_port_assignment_is_rejected(tmp_path):
    tmp_path.chmod(0o700)
    class Cli:
        def run(self, argv):
            if argv[:2] == ['token', 'issue']:
                return {'project_id': 'project1'}
            assert argv == ['port', 'show', 'foreign-port']
            return {'id': 'foreign-port', 'project_id': 'other'}
    with pytest.raises(control.ProtocolError):
        control.execute(request(['floating', 'ip', 'create', '--port', 'foreign-port', 'external-network']), CONFIG, Cli(), tmp_path)
    assert not (tmp_path / 'job-job1.json').exists()


def test_create_positional_must_be_last_to_prevent_reference_scope_bypass():
    with pytest.raises(control.ProtocolError):
        control.decode(json.dumps(request(['security', 'group', 'rule', 'create', 'foreign-group', '--remote-group', 'owned-group'])).encode())



def test_limits_show_adds_fixed_absolute_without_exposing_cli_options(tmp_path):
    calls = []
    class Cli:
        def run(self, argv):
            calls.append(argv)
            return {'project_id': 'project1'} if argv == ['token', 'issue'] else [{'Name': 'maxTotalInstances', 'Value': 10}]
    assert control.execute(request(['limits', 'show']), CONFIG, Cli(), tmp_path) == [{'Name': 'maxTotalInstances', 'Value': 10}]
    assert calls == [['token', 'issue'], ['limits', 'show', '--absolute']]
    for flag in ('--absolute', '--rate', '--project'):
        with pytest.raises(control.ProtocolError):
            control.decode(json.dumps(request(['limits', 'show', flag])).encode())
