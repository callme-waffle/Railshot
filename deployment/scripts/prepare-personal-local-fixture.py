#!/usr/bin/env python3
"""Create isolated, local-only credentials and config for production.compose.yaml."""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
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


def tls_fixture(output):
    """A local CA is trusted explicitly by test hosts, never by the installer."""
    def openssl(*args):
        subprocess.run(['openssl', *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    openssl('req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '7',
            '-subj', '/CN=Railshot E2E private test CA', '-keyout', str(output / 'tls-ca-key.pem'),
            '-out', str(output / 'tls-ca.pem'))
    openssl('req', '-newkey', 'rsa:3072', '-nodes', '-subj', '/CN=localhost',
            '-keyout', str(output / 'tls-key.pem'), '-out', str(output / 'tls.csr'))
    private(output / 'tls.ext', 'subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n')
    openssl('x509', '-req', '-in', str(output / 'tls.csr'), '-CA', str(output / 'tls-ca.pem'),
            '-CAkey', str(output / 'tls-ca-key.pem'), '-CAcreateserial', '-days', '7',
            '-extfile', str(output / 'tls.ext'), '-out', str(output / 'tls-cert.pem'))
    for name in ('tls-ca-key.pem', 'tls-ca.pem', 'tls-key.pem', 'tls-cert.pem'):
        (output / name).chmod(0o600)
    for name in ('tls.csr', 'tls.ext', 'tls-ca.srl'):
        (output / name).unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--public-url', default='http://dashboard:8080')
    parser.add_argument('--gateway-endpoint', default='gateway:51820')
    args = parser.parse_args()
    parsed = urlsplit(args.public_url)
    if parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'dashboard', 'localhost', '127.0.0.1'} \
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
    private(output / 'mail-token', token(32) + '\n')
    private(output / 'private-key', wireguard_key() + '\n')
    subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072',
                    '-out', str(output / 'personal-vault-private.pem')],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (output / 'personal-vault-private.pem').chmod(0o600)
    base = args.public_url.rstrip('/')
    config = {'version': 1, 'public_url': base,
              'installer_url': f'{base}/personal/{digest}/install.sh',
              'artifact_url': f'{base}/personal/{digest}/personal-client.tgz',
              'artifact_sha256': digest,
              'gateway': {'config_path': '/var/lib/railshot-personal-gateway/config.json',
                          'service_host': 'gateway', 'service_port': 4184,
                          'token_file': '/var/lib/railshot/config/personal-gateway-token'},
              'vault': {'private_key_file': '/var/lib/railshot/config/personal-vault-private.pem'}}
    private(output / 'personal.json', json.dumps(config, sort_keys=True) + '\n')
    tls_fixture(output)
    print(json.dumps({'status': 'created', 'directory': str(output), 'release': digest,
                      'gateway_endpoint': args.gateway_endpoint}, sort_keys=True))


if __name__ == '__main__':
    main()
