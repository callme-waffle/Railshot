#!/usr/bin/env python3
"""Initialize fixed private volumes; public endpoint changes require explicit editing.

Never prints keys, tokens or state. Runs only in the local acceptance container.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

sys.path.insert(0, '/opt/railshot/deployment/scripts')
from personal_wireguard import private, save, validate


def require(value):
    if not value:
        raise ValueError('Container configuration or volume ownership differs')


def directory(path, uid=0, gid=0):
    require(not any(p.is_symlink() for p in (path, *path.parents)))
    if path.exists():
        info = path.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, uid) and not info.st_mode & 0o022)
        # An imported, nonempty API volume must already belong to the API UID.
        require(info.st_uid == uid or not any(path.iterdir()))
    else:
        path.mkdir(parents=True, mode=0o700)
    os.chown(path, uid, gid)
    path.chmod(0o700)


def document(path, expected, uid=0, gid=0):
    require(not path.is_symlink())
    if path.exists():
        actual = json.loads(private(path, owner=uid))
        require(actual == expected)
        return
    # Fixed root-owned initializer writes once; never replaces stored API state.
    fd, temporary = tempfile.mkstemp(prefix='.initialize-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(expected, stream, sort_keys=True)
            os.fchown(stream.fileno(), uid, gid)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def endpoint(name, allow_http):
    value = os.environ[name].rstrip('/')
    parsed = urlsplit(value)
    require(parsed.scheme == 'https' or allow_http and parsed.scheme == 'http')
    require(parsed.hostname and not parsed.username and not parsed.password and not parsed.query
            and not parsed.fragment and not parsed.path and not any(c.isspace() for c in value))
    return value


def main():
    require(os.geteuid() == 0)
    account = pwd.getpwnam('railshot')
    require(account.pw_uid == account.pw_gid == 1000)
    root = Path('/var/lib/railshot')
    directory(root, account.pw_uid, account.pw_gid)
    directory(root / 'config', account.pw_uid, account.pw_gid)
    for path in ('/etc/wireguard', '/etc/railshot-personal-gateway', '/var/lib/railshot-personal-gateway'):
        directory(Path(path))
    key = Path('/etc/railshot-personal-gateway/private.key')
    if not key.exists():
        secret = subprocess.run(['wg', 'genkey'], capture_output=True, text=True, check=True).stdout
        save(key, secret)
    else:
        private(key)
    gateway = {'version': 1, 'lifecycle': 'direct', 'interface': 'railshotwg',
        'address_pool': '10.253.240.0/24', 'server_address': '10.253.240.1/32',
        'endpoint': os.environ['PERSONAL_GATEWAY_ENDPOINT'], 'listen_port': 51820,
        'private_key_file': str(key), 'ledger_path': '/var/lib/railshot-personal-gateway/peers.json',
        'request_root': '/var/lib/railshot/personal/gateway', 'request_owner_uid': 1000}
    validate(gateway)
    document(Path('/etc/railshot-personal-gateway/config.json'), gateway)
    allow_http = os.environ.get('RAILSHOT_PERSONAL_TEST_ALLOW_HTTP') == '1'
    base = endpoint('PERSONAL_ARTIFACT_BASE_URL', allow_http)
    config = {'version': 1, 'public_url': endpoint('PERSONAL_PUBLIC_URL', allow_http),
        'installer_url': base + '/install.sh', 'artifact_url': base + '/personal-client.tgz',
        'artifact_sha256': hashlib.sha256(Path('/opt/railshot/public/personal-client.tgz').read_bytes()).hexdigest(),
        'gateway': {'command': '/usr/local/sbin/railshot-personal-gateway',
                    'config_path': '/etc/railshot-personal-gateway/config.json'}}
    runtime_config = os.environ.get('PERSONAL_RUNTIME_CONFIG_FILE', '')
    if runtime_config:
        runtime_path = Path(runtime_config)
        require(runtime_path.is_absolute() and not any(p.is_symlink() for p in (runtime_path, *runtime_path.parents)))
        private(runtime_path, owner=account.pw_uid)
        config['runtime'] = {'config_path': str(runtime_path)}
    document(root / 'config/personal.json', config, account.pw_uid, account.pw_gid)
    print('Private gateway volumes and pinned personal client artifact verified.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Container initialization refused: inspect endpoint settings and private volume ownership.', file=sys.stderr)
        raise SystemExit(1) from None
