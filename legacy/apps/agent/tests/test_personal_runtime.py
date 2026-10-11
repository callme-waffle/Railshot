import importlib.util
import base64
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from apps.agent import personal_runtime as runtime


@pytest.fixture
def private(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path


def test_copy_ssh_input_accepts_owned_public_hosts_but_not_public_private_key(private, monkeypatch):
    source = private / 'source'
    source.write_text('chosen ssh data')
    source.chmod(0o644)
    with pytest.raises(ValueError, match='RUNTIME_SSH_INPUT_UNSAFE'):
        runtime.import_ssh_reference(str(source), private / 'key', identity=True)
    copied = Path(runtime.import_ssh_reference(str(source), private / 'hosts', identity=False))
    assert copied.read_text() == source.read_text() and copied.stat().st_mode & 0o077 == 0
    source.chmod(0o600)
    runtime.import_ssh_reference(str(source), private / 'key', identity=True)
    source.write_text('changed')
    with pytest.raises(ValueError, match='RUNTIME_SSH_REFERENCE_CHANGED'):
        runtime.import_ssh_reference(str(source), private / 'key', identity=True)


def test_ssh_symlink_is_rejected(private):
    target = private / 'target'
    target.write_text('key'); target.chmod(0o600)
    link = private / 'link'; link.symlink_to(target)
    with pytest.raises(ValueError, match='RUNTIME_SSH_INPUT_UNSAFE'):
        runtime.import_ssh_reference(str(link), private / 'copy', identity=True)


def test_unknown_creation_is_not_replayed(private):
    calls = []
    def execute(argv):
        calls.append(argv)
        raise TimeoutError()
    cli = SimpleNamespace(run=execute)
    with pytest.raises(TimeoutError):
        runtime.create_once(cli, private, 'server', ['server', 'create', 'runtime'])
    with pytest.raises(ValueError, match='RUNTIME_CREATE_OUTCOME_UNKNOWN'):
        runtime.create_once(cli, private, 'server', ['server', 'create', 'runtime'])
    assert len(calls) == 1


def test_successful_creation_reuses_only_same_record_and_omits_provider_secrets(private):
    calls = []
    cli = SimpleNamespace(run=lambda argv: calls.append(argv) or {'id': 'server1', 'name': 'runtime', 'adminPass': 'secret'})
    argv = ['server', 'create', 'runtime']
    assert runtime.create_once(cli, private, 'server', argv) == {'id': 'server1', 'name': 'runtime'}
    runtime.create_once(cli, private, 'server', argv)
    assert len(calls) == 1 and 'secret' not in (private / 'runtime-create.json').read_text()
    with pytest.raises(ValueError, match='RUNTIME_CREATE_OUTCOME_UNKNOWN'):
        runtime.create_once(cli, private, 'server', ['server', 'create', 'other'])


@pytest.mark.parametrize('raw', [
    'private=10.0.0.17', {'private': [{'addr': '10.0.0.17', 'version': 4}]},
    {'private': ['10.0.0.17']}, [{'network': 'private', 'address': '10.0.0.17', 'version': 4}],
])
def test_actual_openstack_address_shapes(raw):
    assert runtime.server_addresses({'addresses': raw}) == [{'network': 'private', 'address': '10.0.0.17', 'version': 4}]


def test_public_or_loopback_is_not_a_runtime_private_address():
    for value in ('network=127.0.0.1', 'network=8.8.8.8'):
        with pytest.raises(ValueError, match='RUNTIME_PRIVATE_ADDRESS_REQUIRED'):
            runtime.server_addresses({'addresses': value})


def config():
    return {'target_id': 'personal-00000000-0000-0000-0000-000000000001', 'generation': 1, 'project_id': 'project1',
            'resource_id': 'server1', 'management_network': 'private', 'placement': 'nova', 'architecture': 'amd64',
            'initialization': 'preconfigured', 'private_ipv4': '10.0.0.17',
            'ssh': {'user': 'ubuntu', 'host': '10.0.0.17', 'port': 22, 'identity_file': '/key', 'known_hosts_file': '/hosts'}}


def test_healthy_existing_k3s_never_calls_installer(private, monkeypatch):
    monkeypatch.setattr(runtime, 'inspect_vm', lambda *args: {'present': True, 'ready': True})
    def forbidden(*args, **kwargs):
        raise AssertionError('Must not install over a healthy existing cluster')
    assert runtime.ensure_cluster(config(), {}, private, runner=forbidden, adapter=SimpleNamespace(from_openstack=forbidden)) == {'reused': True}


def test_absent_k3s_uses_existing_ansible_runtime_install(private, monkeypatch):
    reads = iter([{'present': False, 'ready': False}, {'present': True, 'ready': True}])
    monkeypatch.setattr(runtime, 'inspect_vm', lambda *args: next(reads))
    calls = []
    adapter = SimpleNamespace(from_openstack=lambda server, **kwargs: calls.append(kwargs) or kwargs,
        run=lambda request, **kwargs: calls.append(kwargs) or {'status': 'succeeded', 'runtime_ready': True})
    assert runtime.ensure_cluster(config(), {}, private, adapter=adapter) == {'reused': False}
    assert calls[0]['operation'] == 'runtime.install'
    assert calls[1]['state_dir'] == private / 'runtime-ansible'


def test_ansible_unknown_is_not_reported_ready(private, monkeypatch):
    monkeypatch.setattr(runtime, 'inspect_vm', lambda *args: {'present': False, 'ready': False})
    adapter = SimpleNamespace(from_openstack=lambda *args, **kwargs: kwargs, run=lambda *args, **kwargs: {'status': 'blocked'})
    with pytest.raises(ValueError, match='RUNTIME_ANSIBLE_NOT_READY'):
        runtime.ensure_cluster(config(), {}, private, adapter=adapter)


def test_management_host_ip_is_rejected_before_ssh(monkeypatch):
    monkeypatch.setattr(runtime, 'local_addresses', lambda *args: {'10.0.0.17'})
    with pytest.raises(ValueError, match='RUNTIME_MANAGEMENT_HOST_FORBIDDEN'):
        runtime.inspect_vm(config(), runner=lambda *args, **kwargs: pytest.fail('SSH must not run'))


def test_management_machine_id_or_devstack_is_rejected(monkeypatch):
    monkeypatch.setattr(runtime, 'local_addresses', lambda *args: set())
    monkeypatch.setattr(runtime, 'ssh_command', lambda *args: ['ssh'])
    monkeypatch.setattr(Path, 'read_text', lambda *args: 'same-machine')
    for machine, management in [('same-machine', False), ('different-machine', True)]:
        with pytest.raises(ValueError, match='RUNTIME_MANAGEMENT_HOST_FORBIDDEN'):
            runtime.inspect_vm(config(), runner=lambda *args, **kwargs: json.dumps({'machine_id': machine, 'management_host': management}))


def test_broken_existing_cluster_never_becomes_absent(monkeypatch):
    monkeypatch.setattr(runtime, 'local_addresses', lambda *args: set())
    monkeypatch.setattr(runtime, 'ssh_command', lambda *args: ['ssh'])
    monkeypatch.setattr(Path, 'read_text', lambda *args: 'local')
    response = {'machine_id': 'remote', 'management_host': False, 'architecture': 'x86_64', 'present': True, 'ready': False}
    with pytest.raises(ValueError, match='RUNTIME_EXISTING_CLUSTER_UNHEALTHY'):
        runtime.inspect_vm(config(), runner=lambda *args, **kwargs: json.dumps(response))


def test_runtime_package_has_exact_reviewed_ansible_inputs(private):
    import tarfile
    spec = importlib.util.spec_from_file_location('runtime_packaging', ROOT / 'deployment/scripts/package-personal-client.py')
    package = importlib.util.module_from_spec(spec); spec.loader.exec_module(package)
    result = package.package(ROOT, private / 'payload.tgz')
    with tarfile.open(result['artifact_path']) as archive:
        names = set(archive.getnames())
    assert set(package.RUNTIME_FILES) <= names
    assert 'infrastructure/ansible/runtime.yml' in names
    assert not any('inventories/' in path or 'group_vars/' in path or 'vault' in path for path in names)
    assert 'apps/agent/personal_runtime.py' in names


def public_key():
    return 'ssh-ed25519 ' + base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + bytes(range(32))).decode()


def test_console_pin_is_bound_to_created_project_and_instance():
    cfg = config()
    marker = 'RAILSHOT_VM_HOST_KEY:railshot-' + cfg['target_id'][-32:] + '-1 '
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        assert argv[3:] == ['console', 'log', 'show', 'server1', '--lines', '200']
        assert '--format' not in argv
        return SimpleNamespace(returncode=0, stdout='boot output\n' + marker + public_key() + '\n', stderr='')
    cli = SimpleNamespace(auth={'auth_url': 'https://example.test'}, timeout=60, runner=runner,
        run=lambda argv: {'id': 'server1', 'project_id': 'project1'})
    assert runtime.console_host_key(cli, 'server1', cfg) == public_key()
    assert len(calls) == 1
    cli.run = lambda argv: {'id': 'server1', 'project_id': 'foreign'}
    with pytest.raises(ValueError, match='RUNTIME_SERVER_PROJECT_MISMATCH'):
        runtime.console_host_key(cli, 'server1', cfg)
    assert len(calls) == 1


def test_console_without_matching_marker_never_falls_back_to_unverified_key():
    cli = SimpleNamespace(auth={}, timeout=60,
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=public_key(), stderr=''),
        run=lambda argv: {'id': 'server1', 'project_id': 'project1'})
    with pytest.raises(ValueError, match='RUNTIME_CONSOLE_HOST_KEY_UNAVAILABLE'):
        runtime.console_host_key(cli, 'server1', config(), timeout=0)


def test_new_vm_uses_only_explicit_project_resources_and_minimal_cloud_init(private, monkeypatch):
    public = public_key()
    def runner(argv, **kwargs):
        path = Path(argv[-1]); path.write_text('private test key'); path.chmod(0o600)
        path.with_suffix('.pub').write_text(public)
        return ''
    def cloud(argv):
        if argv[:2] == ['network', 'show']: return {'project_id': 'project1'}
        if argv[:2] == ['image', 'show']: return {'status': 'active', 'visibility': 'public'}
        if argv[:2] == ['flavor', 'show']: return {'vcpus': 2, 'ram': 4096, 'disk': 40}
        if argv[:2] == ['keypair', 'show']: return {'public_key': public}
        if argv[:3] == ['security', 'group', 'show']: return {'project_id': 'project1'}
        raise AssertionError(argv)
    changes = []
    def created(cli, directory, stage, argv):
        changes.append((stage, argv))
        return {'id': 'group1' if stage == 'security-group' else 'server1', 'name': 'runtime'}
    monkeypatch.setattr(runtime, 'create_once', created)
    plan = {'image_id': 'image1', 'flavor_id': '3', 'network_id': 'network1', 'name': 'runtime',
            'ssh_user': 'ubuntu', 'ssh_source_cidr': '10.0.0.1/32'}
    assert runtime.create_vm(SimpleNamespace(run=cloud), config(), plan, private, runner)[0] == 'server1'
    document = json.loads((private / 'runtime-cloud-init.json').read_text().split('\n', 1)[1])
    assert document['users'][0]['ssh_authorized_keys'] == [public]
    assert set(document) == {'users', 'ssh_pwauth', 'disable_root', 'runcmd'}
    assert 'k3s' not in json.dumps(document) and 'RAILSHOT_VM_HOST_KEY:' in json.dumps(document)
    ingress = [argv for stage, argv in changes if stage.startswith('ingress-')]
    assert [argv[argv.index('--dst-port') + 1] for argv in ingress] == ['22', '6443']
    assert all(argv[argv.index('--remote-ip') + 1] == '10.0.0.1/32' for argv in ingress)
