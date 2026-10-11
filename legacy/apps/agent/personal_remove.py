#!/usr/bin/env python3
"""Standalone removal helper: only fixed, installation-owned local resources."""
import json
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import pwd
import grp
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

MARKER = '# Managed by RailShot personal client v1\n'
PAYLOAD = Path('/opt/railshot/personal')
CONFIG = Path('/etc/railshot-personal')
STATE = Path('/var/lib/railshot-personal')
CLI_HOME = Path('/var/lib/railshot-personal-cli')
CLI_USER = 'railshot-openstack'
UNIT = Path('/etc/systemd/system/railshot-personal.service')
RUNTIME_UNIT = Path('/etc/systemd/system/railshot-personal-runtime.service')
KUBE_UNIT = Path('/etc/systemd/system/railshot-personal-kube.service')
KUBE_PROXY_UNIT = Path('/etc/systemd/system/railshot-personal-kube-proxy.service')
KUBE_HOME = Path('/var/lib/railshot-personal-kube')
KUBE_USER = 'railshot-runtime'
KUBE_WRAPPER = Path('/usr/local/sbin/railshot-runtime-access')
KUBE_SUDOERS = Path('/etc/sudoers.d/railshot-runtime-access')
WG = Path('/etc/wireguard/railshot0.conf')
RUN = Path('/run')
PID_FILES = (Path('/run/railshot-personal-runtime.pid'), Path('/run/railshot-personal-kube.pid'))


class RemovalPreflightError(ValueError):
    """Ownership validation failed before any removal command was invoked."""


def require(value):
    if not value:
        raise ValueError('REMOVAL_OWNERSHIP_UNVERIFIED')


def safe(path, directory=False, owner=None):
    require(not any(p.is_symlink() for p in (path, *path.parents)))
    info = path.stat()
    require(info.st_uid == (os.geteuid() if owner is None else owner) and not info.st_mode & 0o022)
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    require(not path.is_mount())


def run(args):
    # Account database writes can take longer on the supported customer host.
    # Keep the normal deadline for service/tunnel commands.
    timeout = 180 if args in (['userdel', CLI_USER], ['userdel', KUBE_USER], ['groupdel', CLI_USER], ['groupdel', KUBE_USER]) else 45
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    require(result.returncode == 0)
    return result.stdout.strip()


def atomic(path, document):
    safe(path.parent, True)
    if path.exists():
        safe(path)
    fd, temporary = tempfile.mkstemp(prefix='.remove-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(document, stream)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inspect_payload():
    safe(PAYLOAD, True)
    marker = PAYLOAD / '.railshot-personal.json'
    safe(marker)
    manifest = json.loads(marker.read_text())
    require(set(manifest) == {'version', 'files'} and manifest['version'] == 1 and isinstance(manifest['files'], dict))
    files = set(manifest['files'])
    directories = {'.'}
    for name, digest in manifest['files'].items():
        relative = PurePosixPath(name)
        require(isinstance(name, str) and not relative.is_absolute() and '..' not in relative.parts
                and str(relative) == name and name not in ('.', '.railshot-personal.json')
                and not name.startswith('.venv/') and isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest))
        path = PAYLOAD / name
        safe(path)
        require(hashlib.sha256(path.read_bytes()).hexdigest() == digest)
        directories.update(str(parent) for parent in relative.parents)
    for path in PAYLOAD.rglob('*'):
        relative = path.relative_to(PAYLOAD)
        require(not path.is_mount())
        if relative.parts[0] == '.venv':
            # This isolated interpreter belongs to the installer-created payload;
            # symlinks are deleted as links and may only select its own files or Python.
            if path.is_symlink():
                info = path.lstat()
                resolved = path.resolve()
                require(info.st_uid == os.geteuid() and info.st_nlink == 1 and (
                    resolved.is_relative_to(PAYLOAD / '.venv') or re.fullmatch(r'/usr/bin/python3(?:\.[0-9]+)?', str(resolved))))
            else:
                safe(path, path.is_dir())
            continue
        safe(path, path.is_dir())
        name = str(relative)
        if path.is_dir():
            require(name in directories or path.name == '__pycache__' and str(relative.parent) in directories)
        elif name not in files and name != '.railshot-personal.json':
            require(path.parent.name == '__pycache__' and re.fullmatch(r'[A-Za-z0-9_]+\.cpython-[0-9]+(?:\.opt-[0-9]+)?\.pyc', path.name))
            source = path.parent.parent / (path.name.split('.cpython-', 1)[0] + '.py')
            require(str(source.relative_to(PAYLOAD)) in files)
    if (PAYLOAD / '.venv').exists():
        safe(PAYLOAD / '.venv/pyvenv.cfg')


def helper_active(operation_id, attempt_id=None, runner=run):
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', operation_id))
    require(attempt_id is None or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', attempt_id))
    unit = 'railshot-remove-' + operation_id + ('.' + attempt_id if attempt_id else '')
    try:
        rows = runner(['systemctl', 'list-units', '--all', '--full', '--no-legend', '--plain', unit + '.service']).splitlines()
        require(len(rows) <= 1)
        if not rows:
            return False  # --collect removes completed transient units entirely.
        columns = rows[0].split()
        require(len(columns) >= 4 and columns[0] == unit + '.service')
        return columns[2] not in ('inactive', 'failed')
    except Exception:
        return None  # Unknown is neither progress nor permission to retry.


def inspect_intact(config, operation_id, attempt_id=None, runner=run):
    active = helper_active(operation_id, attempt_id, runner) is not False
    result = {'state': 'unknown', 'preflight_ok': False, 'helper_active': active}
    try:
        inspect(config)
        result['preflight_ok'] = True
        paths = [PAYLOAD, CONFIG, STATE, UNIT, WG, PAYLOAD / '.venv/pyvenv.cfg', PAYLOAD / '.venv/bin/python']
        units = ['railshot-personal', 'wg-quick@railshot0']
        if config.get('runtime_access'):
            paths.extend([RUNTIME_UNIT, CLI_HOME, CONFIG / 'cli-account.json'])
            units.append('railshot-personal-runtime')
        if (CONFIG / 'runtime.json').exists():
            paths.extend([KUBE_UNIT, KUBE_PROXY_UNIT, KUBE_HOME, KUBE_WRAPPER, KUBE_SUDOERS, CONFIG / 'runtime-access-account.json'])
            units.extend([KUBE_UNIT.stem, KUBE_PROXY_UNIT.stem])
        intact = all(path.exists() for path in paths)
        identity = operation_id + ('.' + attempt_id if attempt_id else '')
        # Failed transient-service admission can leave private staged helpers.
        # A new attempt must not hide those leftovers or race a delayed launch.
        intact = intact and not any((RUN / ('railshot-remove-' + identity + suffix)).exists()
                                    for suffix in ('.py', '.json'))
        intact = intact and all(runner(['systemctl', 'show', unit, '--property=ActiveState', '--value']) == 'active' for unit in units)
        intact = intact and 'railshot0' in runner(['wg', 'show', 'interfaces']).split()
        result['state'] = 'intact' if intact and not active else 'partial' if not active else 'unknown'
    except Exception:
        pass
    return result


def inspect_cli_cache(owner):
    # OpenStack's Python entry-point discovery creates this cache under HOME.
    # Accept only that known layout, owned by the dedicated CLI account; never
    # expand removal to arbitrary hidden directories, links, or mounted data.
    cache = CLI_HOME / '.cache'
    if not cache.exists() and not cache.is_symlink():
        return
    safe(cache, True, owner)
    require(not cache.is_mount())
    require(all(path.name == 'python-entrypoints' for path in cache.iterdir()))
    entries = cache / 'python-entrypoints'
    if entries.exists() or entries.is_symlink():
        safe(entries, True, owner)
        require(not entries.is_mount())
        for path in entries.iterdir():
            require(re.fullmatch(r'[0-9a-f]{64}', path.name))
            safe(path, owner=owner)
            require(not path.is_mount())


def inspect(config):
    # Validate every deletion target before making any change. No paths are taken
    # from the remote command; namespaces, VMs and provider resources are excluded.
    for path in (UNIT, RUNTIME_UNIT, KUBE_UNIT, KUBE_PROXY_UNIT, WG, KUBE_SUDOERS):
        if path.exists():
            safe(path)
            require(path.read_text().startswith(MARKER))
    if KUBE_WRAPPER.exists():
        safe(KUBE_WRAPPER)
        require(KUBE_WRAPPER.read_text().startswith('#!/bin/sh\n' + MARKER))
    if PAYLOAD.exists():
        inspect_payload()
    if CONFIG.exists():
        safe(CONFIG, True)
        safe(CONFIG / 'client.json')
        stored = json.loads((CONFIG / 'client.json').read_text())
        require(all(stored.get(k) == config.get(k) for k in ('target_id', 'generation', 'client_token')))
        require(set(p.name for p in CONFIG.iterdir()) <= {'client.json', 'credentials.key', 'credentials.enc', 'identity-owner', 'wireguard.key', 'runtime_host_key', 'runtime_host_key.pub', 'runtime_authorized_keys', 'runtime_sshd_config', 'cli-account.json',
                'runtime-plan.json', 'runtime-create.json', 'runtime-cloud-init.json', 'runtime.json', 'runtime-evidence.json', 'runtime_id_ed25519', 'runtime_id_ed25519.pub', 'runtime_known_hosts',
                'runtime-access-account.json', 'runtime_access_host_key', 'runtime_access_host_key.pub', 'runtime_access_sshd_config',
                'runtime-access-objects.json', 'runtime-access.lock'})
        for path in CONFIG.iterdir():
            safe(path, path.is_dir())
            if path.name == 'identity-owner':
                require(set(child.name for child in path.iterdir()) <= {'credentials.key', 'credentials.enc'})
                for child in path.iterdir():
                    safe(child)
    marker = CONFIG / 'cli-account.json'
    if marker.exists():
        safe(marker)
        owned = json.loads(marker.read_text())
        account = pwd.getpwnam(CLI_USER)
        require(owned == {'user': CLI_USER, 'uid': account.pw_uid, 'gid': account.pw_gid}
                and account.pw_uid != 0 and account.pw_dir == str(CLI_HOME))
        if CLI_HOME.exists():
            safe(CLI_HOME, True, account.pw_uid)
            require(all(p.name in ('credentials.key', 'credentials.enc', 'control.json', 'authorized_keys', 'resources.json', 'jobs.lock', '.cache')
                        or p.name.startswith('job-') and p.suffix == '.json' for p in CLI_HOME.iterdir()))
            inspect_cli_cache(account.pw_uid)
            for path in CLI_HOME.iterdir():
                if path.name != '.cache':
                    safe(path, owner=account.pw_uid)
    else:
        require(not CLI_HOME.exists())
    marker = CONFIG / 'runtime-access-account.json'
    if marker.exists():
        safe(marker)
        account = pwd.getpwnam(KUBE_USER)
        require(json.loads(marker.read_text()) == {'user': KUBE_USER, 'uid': account.pw_uid, 'gid': account.pw_gid}
                and account.pw_uid != 0 and account.pw_dir == str(KUBE_HOME))
        if KUBE_HOME.exists():
            safe(KUBE_HOME, True)
            require(set(p.name for p in KUBE_HOME.iterdir()) <= {'authorized_keys'})
            for path in KUBE_HOME.iterdir():
                safe(path)
    else:
        require(not KUBE_HOME.exists() and not any(p.exists() for p in (KUBE_UNIT, KUBE_PROXY_UNIT, KUBE_WRAPPER, KUBE_SUDOERS)))
    if STATE.exists():
        safe(STATE, True)
        require(all(p.name in ('daemon.lock', 'runtime-ansible', 'identity-ownership.json',
                              'identity-progress.json', 'identity-revocation.json')
                    or p.name.startswith('job-') and p.suffix == '.json' for p in STATE.iterdir()))
        for path in STATE.iterdir():
            if path.name != 'runtime-ansible':
                safe(path)
        runtime_state = STATE / 'runtime-ansible'
        if runtime_state.exists():
            require((CONFIG / 'runtime.json').exists())
            safe(runtime_state, True)
            for path in runtime_state.rglob('*'):
                safe(path, path.is_dir())
                require(not path.is_mount())
    for path in PID_FILES:
        if path.exists() or path.is_symlink():
            safe(path)
            require(re.fullmatch(r'[1-9][0-9]*\s*', path.read_text()))


def remove_account(user, marker, home, runner):
    owned = json.loads(marker.read_text())
    runner(['userdel', user])
    try:
        pwd.getpwnam(user)
    except KeyError:
        pass
    else:
        require(False)
    try:
        group = grp.getgrnam(user)
    except KeyError:
        group = None
    if group:
        require(group.gr_gid == owned['gid'] and not group.gr_mem)
        runner(['groupdel', user])
        try:
            grp.getgrnam(user)
        except KeyError:
            pass
        else:
            require(False)
    if home.exists():
        shutil.rmtree(home)
    require(not home.exists() and not home.is_symlink())


def revoke_identity(config):
    interpreter = PAYLOAD / '.venv/bin/python'
    entrypoint = PAYLOAD / 'deployment/bootstrap/revoke_personal_identity.py'
    require(interpreter.is_file())
    safe(entrypoint)
    environment = {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(PAYLOAD / 'deployment/bootstrap') + ':' + str(PAYLOAD),
                   'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([str(interpreter), str(entrypoint), '--config-dir', str(CONFIG),
                             '--state-dir', str(STATE)]
                            + (['--test-allow-http'] if config.get('test_allow_http') is True else []),
                            capture_output=True, text=True, timeout=90, check=False, env=environment)
    require(result.returncode == 0 and len(result.stdout) <= 65536)
    document = json.loads(result.stdout)
    require(isinstance(document, dict))
    return document


def cleanup(config, runner=run, *, steps=None, checkpoint=None, identity_revoker=revoke_identity):
    try:
        inspect(config)
    except Exception:
        raise RemovalPreflightError('REMOVAL_OWNERSHIP_UNVERIFIED') from None
    steps = [] if steps is None else steps
    ownership = STATE / 'identity-ownership.json'
    if ownership.exists():
        # Revoke only a credential whose ownership manifest and separate owner
        # vault match. The administrator password is never retained, and the
        # shared project, service user and role metadata remain in OpenStack.
        identity = identity_revoker(config)
        require(identity.get('application_credential') in ('revoked', 'externally_owned'))
        require(set(identity.get('retained', [])) <= {
            'external_identity', 'service_user', 'shared_project', 'role_assignment'})
        steps.append({'name': 'openstack-application-credential', 'status': 'succeeded',
                      'result': identity['application_credential'], 'retained': identity.get('retained', [])})
    if checkpoint:
        checkpoint('mutating')  # Durable intent precedes stopping our parent daemon.
    def stop(unit):
        runner(['systemctl', 'disable', '--now', unit])
        status = runner(['systemctl', 'show', unit, '--property=ActiveState', '--value'])
        require(status in ('inactive', 'failed'))
        if status == 'failed':
            runner(['systemctl', 'reset-failed', unit])
    stop('railshot-personal')
    steps.append({'name': 'client-service', 'status': 'succeeded'})
    if RUNTIME_UNIT.exists():
        stop('railshot-personal-runtime')
    for unit in (KUBE_UNIT, KUBE_PROXY_UNIT):
        if unit.exists():
            stop(unit.stem)
    stop('wg-quick@railshot0')
    require('railshot0' not in runner(['wg', 'show', 'interfaces']).split())
    for path in (UNIT, RUNTIME_UNIT, KUBE_UNIT, KUBE_PROXY_UNIT, KUBE_WRAPPER, KUBE_SUDOERS, WG):
        if path.exists():
            path.unlink()
    runner(['systemctl', 'daemon-reload'])
    for path in PID_FILES:
        if path.exists():
            try:
                os.kill(int(path.read_text().strip()), 0)
            except ProcessLookupError:
                path.unlink()
            else:
                require(False)  # Never signal a possibly reused/customer process ID.
    steps.append({'name': 'client-tunnel', 'status': 'succeeded'})
    if (CONFIG / 'cli-account.json').exists():
        remove_account(CLI_USER, CONFIG / 'cli-account.json', CLI_HOME, runner)
    if (CONFIG / 'runtime-access-account.json').exists():
        remove_account(KUBE_USER, CONFIG / 'runtime-access-account.json', KUBE_HOME, runner)
    for path in (PAYLOAD, CONFIG, STATE):
        if path.exists():
            shutil.rmtree(path)
        require(not path.exists() and not path.is_symlink())
    require(not any(path.exists() or path.is_symlink() for path in (UNIT, RUNTIME_UNIT, KUBE_UNIT, KUBE_PROXY_UNIT, KUBE_WRAPPER, KUBE_SUDOERS, WG, *PID_FILES)))
    steps.append({'name': 'client-files', 'status': 'succeeded'})
    return steps


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('HTTP_REDIRECT_REJECTED')


def send(config, result):
    base = config['api_url']
    parsed = urllib.parse.urlsplit(base)
    schemes = ('https', 'http') if config.get('test_allow_http') is True else ('https',)
    require(parsed.scheme in schemes and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and not any(c.isspace() for c in base))
    request = urllib.request.Request(base.rstrip('/') + '/api/v1/targets/' + config['target_id'] + '/receipts',
                                    data=json.dumps(result).encode(), method='POST', headers={
                                    'Authorization': 'Bearer ' + config['client_token'], 'Content-Type': 'application/json'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=45) as response:
        require(200 <= response.status < 300)


def send_with_retries(config, result, sender=send, sleeper=time.sleep):
    # Retransmit the same receipt only; never replay cleanup after a lost reply.
    for attempt in range(3):
        try:
            sender(config, result)
            return
        except Exception:
            if attempt == 2:
                raise
            sleeper(2)


def main():
    config_path = Path(sys.argv[1])
    self_path = Path(__file__)
    safe(config_path)
    require(config_path.stat().st_mode & 0o077 == 0)
    config = json.loads(config_path.read_text())
    result = {'operation_id': config['operation_id'], 'generation': config['generation'],
              'status': 'unknown', 'steps': [], 'residuals': ['client_removal_unverified'], 'client_removed': False}
    if config.get('attempt_id'):
        result['attempt_id'] = config['attempt_id']
    job_path = STATE / ('job-' + config['operation_id'] + '.json')
    mutation_started = False
    def checkpoint(stage):
        nonlocal mutation_started
        require(job_path.exists())
        safe(job_path)
        job = json.loads(job_path.read_text())
        require(job.get('attempt_id') == config.get('attempt_id'))
        job['status'] = stage
        atomic(job_path, job)
        mutation_started = True
    try:
        cleanup(config, steps=result['steps'], checkpoint=checkpoint)
        # Remove temporary secret and executable before attesting completion.
        config_path.unlink()
        self_path.unlink()
        require(not config_path.exists() and not self_path.exists())
        result.update(status='succeeded', residuals=[], client_removed=True)
    except Exception as error:
        # No receipt of success is emitted for partial cleanup.
        print('RailShot removal stopped before changes: ownership verification failed.'
              if isinstance(error, RemovalPreflightError) else
              'RailShot removal incomplete: manual verification required.', file=sys.stderr)
        if isinstance(error, RemovalPreflightError):
            result.update(status='blocked', error_code='REMOVAL_PREFLIGHT_FAILED', mutation_started=False)
        else:
            result['mutation_started'] = mutation_started
        if job_path.exists():
            safe(job_path)
            job = json.loads(job_path.read_text())
            if job.get('attempt_id') == config.get('attempt_id'):
                job.update(status=result['status'], receipt=result)
                atomic(job_path, job)
        for path in (config_path, self_path):
            if path.exists():
                path.unlink()
    try:
        send_with_retries(config, result)
    except Exception:
        # Server remains unknown when the last receipt is lost. No local success
        # log is substituted for server acknowledgement.
        print('RailShot removal receipt could not be delivered.', file=sys.stderr)
        return 1
    return 0 if result['client_removed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
