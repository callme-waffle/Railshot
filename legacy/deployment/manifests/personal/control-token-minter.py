#!/usr/bin/env python3
"""Mint a short local control-plane token without exposing the admin kubeconfig."""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import time

import yaml

ADMIN = Path('/etc/rancher/k3s/k3s.yaml')
OUTPUT = Path('/var/lib/railshot-control-auth')
RUNTIME_PATH = Path('/run/railshot-kubernetes')
GATEWAY_CONFIG = Path('/etc/railshot-personal-gateway/config.json')
USERNAME = 'system:serviceaccount:railshot-system:railshot-product'
LABEL = re.compile(r'[A-Za-z0-9_-]{1,128}')


def require(value, code='LOCAL_CONTROL_AUTH_INVALID'):
    if not value:
        raise ValueError(code)


def private_file(path, *, owner=0):
    path = Path(path)
    require(path.is_absolute() and not path.is_symlink() and path.is_file())
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == owner and not info.st_mode & 0o077)
    return path


def run(args, *, kubeconfig=ADMIN, input=None):
    command = ['kubectl', '--kubeconfig', str(kubeconfig), '--request-timeout=20s', *args]
    result = subprocess.run(command, input=input, text=True, capture_output=True, timeout=30, check=False)
    require(result.returncode == 0, 'LOCAL_CONTROL_AUTH_COMMAND_FAILED')
    return result.stdout.strip()


def can_i(args, *, kubeconfig):
    command = ['kubectl', '--kubeconfig', str(kubeconfig), '--request-timeout=20s', 'auth', 'can-i', *args]
    result = subprocess.run(command, text=True, capture_output=True, timeout=30, check=False)
    answer = result.stdout.strip()
    require((result.returncode, answer) in ((0, 'yes'), (1, 'no')), 'LOCAL_CONTROL_AUTH_COMMAND_FAILED')
    return answer == 'yes'


def restore_gateway(path=GATEWAY_CONFIG):
    private_file(path)
    result = subprocess.run(['python3', '/opt/railshot/deployment/scripts/personal_wireguard.py',
        '--config', str(path), '--restore'], text=True, capture_output=True, timeout=30, check=False)
    require(result.returncode == 0, 'LOCAL_CONTROL_GATEWAY_GUARD_FAILED')
    value = json.loads(result.stdout)
    require(value.get('status') == 'succeeded' and type(value.get('restored_peers')) is int)
    return value['restored_peers']


def jwt_expiry(token):
    require(isinstance(token, str) and token.count('.') == 2 and not re.search(r'\s', token))
    value = token.split('.')[1]
    value += '=' * (-len(value) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(value))
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('LOCAL_CONTROL_AUTH_INVALID') from None
    require(isinstance(claims, dict) and type(claims.get('exp')) is int and type(claims.get('iat')) is int
            and claims['exp'] > claims['iat'])
    return claims['exp']


def ca_data(path=ADMIN):
    private_file(path)
    value = yaml.safe_load(path.read_bytes())
    require(isinstance(value, dict) and isinstance(value.get('clusters'), list) and len(value['clusters']) == 1)
    encoded = value['clusters'][0].get('cluster', {}).get('certificate-authority-data')
    require(isinstance(encoded, str))
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ValueError('LOCAL_CONTROL_AUTH_INVALID') from None
    require(raw.startswith(b'-----BEGIN CERTIFICATE-----') and raw.rstrip().endswith(b'-----END CERTIFICATE-----'))
    return raw


def kubeconfig():
    return {'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'railshot-local-control',
        'clusters': [{'name': 'railshot-local-control', 'cluster': {
            'server': 'https://127.0.0.1:6443',
            'certificate-authority': str(RUNTIME_PATH / 'ca.crt')}}],
        'users': [{'name': 'railshot-product', 'user': {'tokenFile': str(RUNTIME_PATH / 'token')}}],
        'contexts': [{'name': 'railshot-local-control', 'context': {
            'cluster': 'railshot-local-control', 'user': 'railshot-product', 'namespace': 'argocd'}}]}


def atomic(path, data, *, uid, mode=0o600):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.control-auth-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchown(stream.fileno(), uid, uid)
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify(auth_path, expected_expiry, *, clock=time.time):
    identity = json.loads(run(['auth', 'whoami', '-o', 'json'], kubeconfig=auth_path))
    require(identity.get('status', {}).get('userInfo', {}).get('username') == USERNAME)
    checks = [
        ('get', 'roles/railshot-credentials', 'argocd', True),
        ('get', 'roles/railshot-product-registrations', 'argocd', True),
        ('create', 'secrets', 'argocd', True),
        ('create', 'clusterroles', None, False),
        ('get', 'secrets', 'kube-system', False),
    ]
    results = []
    for verb, resource, namespace, expected in checks:
        args = [verb, resource]
        if namespace:
            args.extend(['-n', namespace])
        allowed = can_i(args, kubeconfig=auth_path)
        require(allowed is expected)
        results.append({'verb': verb, 'resource': resource, 'namespace': namespace, 'allowed': allowed})
    require(expected_expiry - int(clock()) >= 3000)
    return results


def mint(*, output=OUTPUT, uid=1000, clock=time.time):
    require(os.geteuid() == 0 and type(uid) is int and uid > 0)
    private_file(ADMIN)
    output = Path(output)
    if output.exists():
        info = output.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, uid) and not info.st_mode & 0o022)
    else:
        output.mkdir(mode=0o700)
    os.chown(output, uid, uid); output.chmod(0o700)
    token = run(['-n', 'railshot-system', 'create', 'token', 'railshot-product', '--duration=1h'])
    expiry = jwt_expiry(token)
    require(3300 <= expiry - int(clock()) <= 3900)
    staging = Path(tempfile.mkdtemp(prefix='.verify-', dir=output))
    try:
        os.chown(staging, uid, uid); staging.chmod(0o700)
        atomic(staging / 'token', (token + '\n').encode(), uid=uid)
        atomic(staging / 'ca.crt', ca_data(), uid=uid, mode=0o644)
        config = kubeconfig()
        # The verification container and gateway mount the same volume at different paths.
        verify_config = json.loads(json.dumps(config))
        cluster = verify_config['clusters'][0]['cluster']
        cluster['certificate-authority'] = str(staging / 'ca.crt')
        verify_config['users'][0]['user']['tokenFile'] = str(staging / 'token')
        atomic(staging / 'kubeconfig', yaml.safe_dump(verify_config, sort_keys=True).encode(), uid=uid)
        checks = verify(staging / 'kubeconfig', expiry, clock=clock)
        atomic(output / 'ca.crt', ca_data(), uid=uid, mode=0o644)
        atomic(output / 'token', (token + '\n').encode(), uid=uid)
        atomic(output / 'kubeconfig', yaml.safe_dump(config, sort_keys=True).encode(), uid=uid)
        receipt = {'version': 1, 'username': USERNAME,
                   'expires_at': datetime.fromtimestamp(expiry, timezone.utc).isoformat(), 'checks': checks}
        atomic(output / 'receipt.json', (json.dumps(receipt, sort_keys=True) + '\n').encode(), uid=uid, mode=0o644)
        return receipt
    finally:
        for path in staging.iterdir():
            path.unlink()
        staging.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--uid', type=int, default=1000)
    parser.add_argument('--interval', type=int, default=2400)
    parser.add_argument('--guard-interval', type=int, default=30)
    parser.add_argument('--gateway-config', type=Path, default=GATEWAY_CONFIG)
    args = parser.parse_args()
    require(300 <= args.interval <= 3000 and 10 <= args.guard_interval <= 300)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    next_mint = 0
    while True:
        peers = restore_gateway(args.gateway_config)
        if time.monotonic() >= next_mint:
            receipt = mint(output=args.output, uid=args.uid)
            next_mint = time.monotonic() + args.interval
            print(json.dumps({'status': 'ready', 'expires_at': receipt['expires_at'],
                              'restored_peers': peers}), flush=True)
        if args.once or stopping:
            return
        deadline = time.monotonic() + args.guard_interval
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(5, max(0, deadline - time.monotonic())))
        if stopping:
            return


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'status': 'blocked', 'code': 'LOCAL_CONTROL_AUTH_UNAVAILABLE'}), flush=True)
        raise SystemExit(1) from None
