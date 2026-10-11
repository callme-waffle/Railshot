import base64
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location('personal_worker_update', Path(__file__).resolve().parents[1] / 'personal-worker-update.py')
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    folder = tmp_path / 'octavia'; folder.mkdir(mode=0o700)
    state = tmp_path / 'state'; state.mkdir(mode=0o700)
    def write(path, data):
        path.write_bytes(data); path.chmod(0o600)
        return path
    worker = write(folder / 'openstack_route_worker.py', b'print("old worker")\n')
    config = write(folder / 'route-config.json', json.dumps({'state_dir': str(state),
        'loadbalancer_id': '11111111-1111-4111-8111-111111111111'}).encode())
    wrapper = write(folder / 'cloud-cli.py', b'# existing authentication wrapper\n')
    write(state / 'app-existing.json', b'{"status":"configured"}')
    lock = write(state / '11111111-1111-4111-8111-111111111111.lock', b'')
    for key, value in [('WORKER', worker), ('CONFIG', config), ('AUTH_WRAPPER', wrapper), ('ROOT_UID', os.geteuid())]:
        monkeypatch.setattr(updater, key, value)
    candidate = b'print("new worker")\n'
    request = {'expected_current_sha256': updater.sha(worker.read_bytes()),
               'candidate_sha256': updater.sha(candidate), 'candidate_base64': base64.b64encode(candidate).decode()}
    return folder, state, lock, request


def test_update_preserves_config_wrapper_and_journals_and_replays_without_write(deployment, monkeypatch):
    folder, state, lock, request = deployment
    before = updater.invariants(state)
    old = updater.WORKER.read_bytes()
    result = updater.apply_update(request)
    assert result['status'] == 'verified'
    assert result['preserved'] == before == updater.invariants(state)
    assert updater.WORKER.read_bytes() == base64.b64decode(request['candidate_base64'])
    assert updater.WORKER.stat().st_mode & 0o777 == 0o600
    backup = Path(result['backup'])
    assert backup.read_bytes() == old and backup.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(updater, 'atomic', lambda *args: pytest.fail('Verified replay must not write files'))
    assert updater.apply_update(request)['status'] == 'already_current'


@pytest.mark.parametrize('change', ['current_hash', 'candidate_hash', 'syntax', 'backup'])
def test_wrong_hash_invalid_python_or_foreign_backup_never_replaces_worker(deployment, change):
    folder, state, lock, request = deployment
    before = updater.WORKER.read_bytes()
    if change == 'current_hash':
        request['expected_current_sha256'] = '0' * 64
    elif change == 'candidate_hash':
        request['candidate_sha256'] = '0' * 64
    elif change == 'syntax':
        bad = b'def invalid syntax\n'
        request.update(candidate_base64=base64.b64encode(bad).decode(), candidate_sha256=updater.sha(bad))
    else:
        backup = updater.WORKER.with_name(updater.WORKER.name + '.backup-' + request['expected_current_sha256'])
        backup.write_text('foreign'); backup.chmod(0o600)
    with pytest.raises((ValueError, SyntaxError)):
        updater.apply_update(request)
    assert updater.WORKER.read_bytes() == before


def test_active_route_lock_blocks_update(deployment):
    folder, state, lock, request = deployment
    before = updater.WORKER.read_bytes()
    with lock.open('r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with pytest.raises(BlockingIOError):
            updater.apply_update(request)
    assert updater.WORKER.read_bytes() == before
    assert not list(folder.glob('*.backup-*'))


def test_symlink_target_is_never_replaced(deployment):
    folder, state, lock, request = deployment
    actual = folder / 'unrelated.py'
    updater.WORKER.rename(actual)
    updater.WORKER.symlink_to(actual)
    with pytest.raises(ValueError, match='UNSAFE_FILE'):
        updater.apply_update(request)
    assert updater.WORKER.is_symlink()


def test_lost_final_receipt_recovers_identical_candidate_after_readback(deployment, monkeypatch):
    folder, state, lock, request = deployment
    actual = updater.atomic
    def interrupted(path, data):
        if path.name.startswith('.worker-update-') and json.loads(data)['status'] == 'verified':
            raise OSError('simulated lost final write')
        return actual(path, data)
    monkeypatch.setattr(updater, 'atomic', interrupted)
    with pytest.raises(OSError):
        updater.apply_update(request)
    assert updater.sha(updater.WORKER.read_bytes()) == request['candidate_sha256']
    monkeypatch.setattr(updater, 'atomic', actual)
    assert updater.apply_update(request)['status'] == 'verified'


def test_remote_command_fixed_and_candidate_uses_stdin(deployment, tmp_path):
    folder, state, lock, request = deployment
    ssh = tmp_path / 'ssh_config'; ssh.write_text('Host octavia\n')
    source = tmp_path / 'candidate.py'; source.write_bytes(base64.b64decode(request['candidate_base64']))
    def runner(command, **kwargs):
        assert command[-2:] == ['octavia', 'sudo -n /usr/bin/python3 -I -']
        assert str(source) not in command and request['candidate_base64'] not in repr(command)
        assert 'BatchMode=yes' in command and 'StrictHostKeyChecking=yes' in command
        assert kwargs['shell'] is False
        compile(kwargs['input'], '<remote-request>', 'exec')
        receipt = {'version': 1, 'status': 'verified', 'previous_sha256': request['expected_current_sha256'],
                   'installed_sha256': request['candidate_sha256'], 'preserved': updater.invariants(state),
                   'backup': str(updater.WORKER.with_name(updater.WORKER.name + '.backup-' + request['expected_current_sha256']))}
        return SimpleNamespace(returncode=0, stdout=json.dumps(receipt), stderr='')
    assert updater.update_remote(ssh, 'octavia', source, request['expected_current_sha256'],
                                 request['candidate_sha256'], runner)['status'] == 'verified'
    def failed(*args, **kwargs):
        raise subprocess.TimeoutExpired('ssh', 120)
    with pytest.raises(ValueError, match='OUTCOME_UNVERIFIED'):
        updater.update_remote(ssh, 'octavia', source, request['expected_current_sha256'], request['candidate_sha256'], failed)
