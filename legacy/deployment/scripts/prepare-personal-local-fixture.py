#!/usr/bin/env python3
"""Create isolated, local-only credentials and config for production.compose.yaml."""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


def private(path, value):
    path.write_text(value)
    path.chmod(0o600)


def package_digest(source):
    script = source / 'deployment/scripts/package-personal-client.py'
    spec = importlib.util.spec_from_file_location('personal_packager', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix='railshot-personal-fixture-') as temporary:
        return module.package(source, Path(temporary) / 'personal-client.tgz')['artifact_sha256']


def wireguard_key():
    value = bytearray(os.urandom(32))
    value[0] &= 248
    value[31] = value[31] & 127 | 64
    return base64.b64encode(value).decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--public-url', default='http://dashboard:8080')
    parser.add_argument('--gateway-endpoint', default='api:51820')
    args = parser.parse_args()
    parsed = urlsplit(args.public_url)
    if parsed.scheme != 'http' or parsed.hostname not in {'dashboard', 'localhost', '127.0.0.1'} \
            or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error('the local fixture only accepts the isolated Compose dashboard URL')
    output = args.output.resolve()
    if output.exists() or output.is_symlink():
        parser.error('output must not exist')
    output.mkdir(mode=0o700, parents=True)
    digest = package_digest(Path(__file__).resolve().parents[2])
    token = lambda length: base64.urlsafe_b64encode(os.urandom(length)).decode().rstrip('=')
    private(output / 'api-token', token(32) + '\n')
    private(output / 'ipc-token', token(32) + '\n')
    private(output / 'private-key', wireguard_key() + '\n')
    base = args.public_url.rstrip('/')
    config = {'version': 1, 'public_url': base,
              'installer_url': f'{base}/personal/{digest}/install.sh',
              'artifact_url': f'{base}/personal/{digest}/personal-client.tgz',
              'artifact_sha256': digest,
              'gateway': {'config_path': '/var/lib/railshot-personal-gateway/config.json',
                          'socket_path': '/run/railshot-personal-gateway/gateway.sock',
                          'token_file': '/var/lib/railshot/config/personal-gateway-token'}}
    private(output / 'personal.json', json.dumps(config, sort_keys=True) + '\n')
    print(json.dumps({'status': 'created', 'directory': str(output), 'release': digest}, sort_keys=True))


if __name__ == '__main__':
    main()
