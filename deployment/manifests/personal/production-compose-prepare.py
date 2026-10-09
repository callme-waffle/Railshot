#!/usr/bin/env python3
"""Materialize operator or local-test inputs into least-privilege Compose volumes."""
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile


def require(value):
    if not value:
        raise ValueError('invalid production compose input')


def source(name, maximum):
    path = Path('/input') / name
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077 and info.st_size <= maximum)
    return path.read_bytes()


def install(directory, name, data, uid, gid, mode=0o600):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(directory, uid, gid)
    os.chmod(directory, 0o700)
    destination = directory / name
    if destination.exists():
        info = destination.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_gid == gid
                and stat.S_IMODE(info.st_mode) == mode and hmac.compare_digest(destination.read_bytes(), data))
        return
    fd, temporary = tempfile.mkstemp(prefix='.prepare-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    require(os.geteuid() == 0)
    api_state = Path('/output/api-state')
    os.chown(api_state, 1000, 1000)
    os.chmod(api_state, 0o700)
    api_token = source('api-token', 4096).strip()
    ipc_token = source('ipc-token', 512).strip()
    key = source('private-key', 128).strip()
    config_raw = source('personal.json', 1024 * 1024)
    vault_key = source('personal-vault-private.pem', 16 * 1024)
    require(re.fullmatch(rb'[\x21-\x7e]{32,4096}', api_token))
    require(re.fullmatch(rb'[A-Za-z0-9._~-]{32,512}', ipc_token))
    require(re.fullmatch(rb'[A-Za-z0-9+/]{43}=', key))
    config = json.loads(config_raw)
    gateway = config.get('gateway', {}) if isinstance(config, dict) else {}
    require(config.get('version') == 1
            and gateway.get('service_host') == 'gateway' and gateway.get('service_port') == 4184
            and 'socket_path' not in gateway and 'command' not in gateway
            and gateway.get('token_file') == '/var/lib/railshot/config/personal-gateway-token'
            and config.get('vault') == {'private_key_file': '/var/lib/railshot/config/personal-vault-private.pem'}
            and vault_key.startswith(b'-----BEGIN PRIVATE KEY-----\n'))
    install('/output/api-state/config', 'personal.json', config_raw, 1000, 1000)
    install('/output/api-state/config', 'personal-gateway-token', ipc_token + b'\n', 1000, 1000)
    install('/output/api-state/config', 'personal-vault-private.pem', vault_key, 1000, 1000)
    install('/output/api-token', 'token', api_token + b'\n', 1000, 1000)
    install('/output/dashboard-token', 'token', api_token + b'\n', 101, 101)
    install('/output/gateway-key', 'private-key', key + b'\n', 0, 1000, 0o440)
    install('/output/gateway-ipc', 'ipc-token', ipc_token + b'\n', 0, 1000, 0o440)
    if Path('/input/mail-token').exists():
        mail_token = source('mail-token', 512).strip()
        require(re.fullmatch(rb'[A-Za-z0-9._~-]{32,512}', mail_token))
        install('/output/api-state/config', 'personal-mail-token', mail_token + b'\n', 1000, 1000)
        if Path('/output/mail-token').exists():
            install('/output/mail-token', 'token', mail_token + b'\n', 1000, 1000)
    if Path('/output/mail-state').exists():
        os.chown('/output/mail-state', 1000, 1000)
        os.chmod('/output/mail-state', 0o700)
    if Path('/output/tls').exists():
        install('/output/tls', 'cert.pem', source('tls-cert.pem', 16 * 1024), 101, 101)
        install('/output/tls', 'key.pem', source('tls-key.pem', 16 * 1024), 101, 101)
    print('Production-like Compose inputs materialized without printing credentials.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Compose preparation refused invalid or changed private input.', file=os.sys.stderr)
        raise SystemExit(1) from None
