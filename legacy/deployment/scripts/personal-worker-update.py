#!/usr/bin/env python3
"""Operator-only, reviewed Octavia worker update through an existing SSH route.

Only WORKER and its private backup/update receipt are written. Cloud credentials,
route configuration, application journals and running services are not changed.
The remote writer uses fixed paths; no request can select a root write target.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

WORKER = Path('/opt/railshot/octavia/openstack_route_worker.py')
CONFIG = Path('/opt/railshot/octavia/route-config.json')
AUTH_WRAPPER = Path('/opt/railshot/octavia/cloud-cli.py')
ROOT_UID = 0
MAX_SIZE = 1024 * 1024


def require(value, code):
    if not value:
        raise ValueError(code)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def private_directory(path):
    require(path.is_absolute() and path.resolve() == path, 'UPDATE_UNSAFE_DIRECTORY')
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == ROOT_UID and not info.st_mode & 0o077,
            'UPDATE_UNSAFE_DIRECTORY')


def read_private(path):
    require(path.is_absolute() and path.resolve() == path, 'UPDATE_UNSAFE_FILE')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_UID and info.st_nlink == 1
                and not info.st_mode & 0o077 and info.st_size <= MAX_SIZE, 'UPDATE_UNSAFE_FILE')
        return stream.read(MAX_SIZE + 1)


def atomic(path, data):
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        read_private(path)
    fd, temporary = tempfile.mkstemp(prefix='.worker-update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def invariants(directory):
    values = {'route_config': sha(read_private(CONFIG)), 'auth_wrapper': sha(read_private(AUTH_WRAPPER))}
    values['application_journals'] = {path.name: sha(read_private(path)) for path in sorted(directory.glob('*.json'))}
    return values


def checked_request(request):
    require(isinstance(request, dict) and set(request) == {'expected_current_sha256', 'candidate_sha256', 'candidate_base64'},
            'UPDATE_REQUEST_INVALID')
    require(all(isinstance(request[key], str) and re.fullmatch(r'[a-f0-9]{64}', request[key])
                for key in ('expected_current_sha256', 'candidate_sha256')), 'UPDATE_HASH_INVALID')
    require(request['expected_current_sha256'] != request['candidate_sha256'], 'UPDATE_NO_CHANGE')
    require(isinstance(request['candidate_base64'], str) and len(request['candidate_base64']) <= MAX_SIZE * 2,
            'UPDATE_CANDIDATE_INVALID')
    candidate = base64.b64decode(request['candidate_base64'], validate=True)
    require(len(candidate) <= MAX_SIZE and sha(candidate) == request['candidate_sha256'], 'UPDATE_CANDIDATE_HASH_MISMATCH')
    compile(candidate, str(WORKER), 'exec')  # Compile only: no candidate code executes.
    return candidate


def apply_update(request):
    require(os.geteuid() == ROOT_UID, 'UPDATE_ROOT_REQUIRED')
    candidate = checked_request(request)
    private_directory(WORKER.parent)
    config_bytes = read_private(CONFIG)
    config = json.loads(config_bytes)
    require(isinstance(config, dict) and isinstance(config.get('state_dir'), str), 'UPDATE_LEGACY_STATE_REQUIRED')
    directory = Path(config['state_dir'])
    private_directory(directory)
    lb = config.get('loadbalancer_id')
    require(isinstance(lb, str) and str(uuid.UUID(lb)) == lb, 'UPDATE_LOADBALANCER_INVALID')
    lock_path = directory / (lb + '.lock')
    # Use the already-created legacy lock; this updater never invents a new
    # authority or changes a base-only deployment into a legacy configuration.
    fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_UID and info.st_nlink == 1
                and not info.st_mode & 0o077, 'UPDATE_UNSAFE_LOCK')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(read_private(CONFIG) == config_bytes, 'UPDATE_CONFIGURATION_CHANGED')
        old_hash, new_hash = request['expected_current_sha256'], request['candidate_sha256']
        backup = WORKER.with_name(WORKER.name + '.backup-' + old_hash)
        receipt_path = WORKER.with_name('.worker-update-' + new_hash + '.json')
        current = read_private(WORKER)
        require(sha(current) in (old_hash, new_hash), 'UPDATE_CURRENT_HASH_MISMATCH')
        previous = json.loads(read_private(receipt_path)) if receipt_path.exists() else None
        if previous:
            require(previous.get('previous_sha256') == old_hash and previous.get('installed_sha256') == new_hash,
                    'UPDATE_RECEIPT_MISMATCH')
        if sha(current) == new_hash:
            require(previous and sha(read_private(backup)) == old_hash, 'UPDATE_HISTORY_UNVERIFIED')
            if previous.get('status') == 'verified':
                return {**previous, 'status': 'already_current'}
            require(previous.get('status') == 'prepared' and invariants(directory) == previous.get('preserved'),
                    'UPDATE_POSTCHECK_UNVERIFIED')
            receipt = {**previous, 'status': 'verified'}
            atomic(receipt_path, json.dumps(receipt, sort_keys=True).encode())
            return receipt
        preserved = invariants(directory)
        if backup.exists() or backup.is_symlink():
            require(sha(read_private(backup)) == old_hash, 'UPDATE_BACKUP_MISMATCH')
        else:
            atomic(backup, current)
        receipt = {'version': 1, 'status': 'prepared', 'previous_sha256': old_hash,
                   'installed_sha256': new_hash, 'backup': str(backup), 'preserved': preserved}
        atomic(receipt_path, json.dumps(receipt, sort_keys=True).encode())
        atomic(WORKER, candidate)
        require(sha(read_private(WORKER)) == new_hash and invariants(directory) == preserved,
                'UPDATE_POSTCHECK_UNVERIFIED')
        receipt['status'] = 'verified'
        atomic(receipt_path, json.dumps(receipt, sort_keys=True).encode())
        return receipt
    finally:
        os.close(fd)


def remote_program(request):
    # Python literals are transported through stdin, never interpolated into a
    # shell command or passed as executable paths. This source has fixed targets.
    source = Path(__file__).read_text()
    return ("import json\nnamespace={'__name__':'railshot_worker_update'}\n"
            + 'exec(compile(' + repr(source) + ", '<reviewed-worker-updater>', 'exec'), namespace)\n"
            + 'try:\n result=namespace[\'apply_update\'](' + repr(request) + ')\n'
            + "except Exception as error:\n print(json.dumps({'status':'unverified','error_type':type(error).__name__})); raise SystemExit(1)\n"
            + 'print(json.dumps(result,sort_keys=True))\n')


def update_remote(ssh_config, host, candidate, old_hash, new_hash, runner=subprocess.run):
    require(isinstance(host, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', host), 'UPDATE_SSH_HOST_INVALID')
    path = Path(ssh_config).resolve(strict=True)
    require(path.is_file(), 'UPDATE_SSH_CONFIGURATION_INVALID')
    request = {'expected_current_sha256': old_hash, 'candidate_sha256': new_hash,
               'candidate_base64': base64.b64encode(Path(candidate).read_bytes()).decode()}
    checked_request(request)
    command = ['ssh', '-F', str(path), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               host, 'sudo -n /usr/bin/python3 -I -']
    try:
        result = runner(command, input=remote_program(request), capture_output=True, text=True,
                        shell=False, timeout=120)
        require(result.returncode == 0 and len(result.stdout) <= MAX_SIZE, 'UPDATE_REMOTE_OUTCOME_UNVERIFIED')
        receipt = json.loads(result.stdout)
        require(isinstance(receipt, dict) and set(receipt) == {
            'version', 'status', 'previous_sha256', 'installed_sha256', 'backup', 'preserved'}
                and receipt.get('version') == 1 and receipt.get('status') in ('verified', 'already_current')
                and receipt.get('previous_sha256') == old_hash and receipt.get('installed_sha256') == new_hash
                and receipt.get('backup') == str(WORKER.with_name(WORKER.name + '.backup-' + old_hash))
                and isinstance(receipt.get('preserved'), dict), 'UPDATE_REMOTE_OUTCOME_UNVERIFIED')
        preserved = receipt['preserved']
        require(set(preserved) == {'route_config', 'auth_wrapper', 'application_journals'}
                and all(isinstance(preserved[key], str) and re.fullmatch(r'[a-f0-9]{64}', preserved[key])
                        for key in ('route_config', 'auth_wrapper'))
                and isinstance(preserved['application_journals'], dict)
                and all(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value)
                        for value in preserved['application_journals'].values()), 'UPDATE_REMOTE_OUTCOME_UNVERIFIED')
        return receipt
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise ValueError('UPDATE_REMOTE_OUTCOME_UNVERIFIED; repeat only the identical reviewed hashes') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-config', required=True)
    parser.add_argument('--host', default='octavia')
    parser.add_argument('--candidate', type=Path, default=Path(__file__).with_name('openstack_route_worker.py'))
    parser.add_argument('--expected-current-sha256', required=True)
    parser.add_argument('--candidate-sha256', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(update_remote(args.ssh_config, args.host, args.candidate,
                                       args.expected_current_sha256, args.candidate_sha256), sort_keys=True))
    except Exception as error:
        print(json.dumps({'status': 'unverified', 'error_type': type(error).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
