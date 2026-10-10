#!/usr/bin/env python3
"""Enable only native /healthz anonymously on a Railshot-owned runtime.

Invoked over its registered SSH transport. No bearer/client credential is copied.
"""
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import time

MARKER = '# Managed by Railshot deployment runtime. Dedicated single-node server only.'
AUTH_PATH = '/etc/rancher/k3s/runtime-healthz-authentication.json'
AUTH = {'apiVersion': 'apiserver.config.k8s.io/v1', 'kind': 'AuthenticationConfiguration',
        'anonymous': {'enabled': True, 'conditions': [{'path': '/healthz'}]}}
ARGUMENT = 'kube-apiserver-arg:\n  - authentication-config=' + AUTH_PATH + '\n'


def require(value, message):
    if not value:
        raise ValueError(message)


def desired_config(before):
    text = before.decode()
    require(text.startswith(MARKER + '\n'), 'Unmanaged K3s configuration')
    if text.endswith(ARGUMENT):
        text = text[:-len(ARGUMENT)]
    require(not re.search(r'^\s*(?:kube-apiserver-arg|token|token-file|server):', text, re.M),
            'Conflicting K3s server or API configuration')
    require('authentication-config' not in text and 'anonymous-auth' not in text,
            'Conflicting authentication configuration')
    return (text.rstrip() + '\n' + ARGUMENT).encode()


def write_atomic(path, data, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def native(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    require(result.returncode == 0, 'Runtime command failed: ' + args[0])
    return result.stdout


def workload_identity():
    data = json.loads(native(['k3s', 'kubectl', '--request-timeout=15s', 'get',
                              'deployments,services', '-A', '-o', 'json']))
    return sorted((item['kind'], item['metadata']['namespace'], item['metadata']['name'],
                   item['metadata']['uid'], hashlib.sha256(json.dumps(item['spec'], sort_keys=True).encode()).hexdigest())
                  for item in data['items'] if not item['metadata']['namespace'].startswith('kube-'))


def check_health(ca, server_name):
    context = ssl.create_default_context(cadata=ca)
    result = {}
    for path, expected in [('/healthz', 200), ('/version', 401), ('/api/v1/secrets', 401)]:
        connection = http.client.HTTPConnection('127.0.0.1', 6443, timeout=5)
        connection.connect()
        connection.sock = context.wrap_socket(connection.sock, server_hostname=server_name)
        connection.request('GET', path)
        response = connection.getresponse()
        body = response.read(1024).decode()
        connection.close()
        require(response.status == expected and (path != '/healthz' or body.strip() == 'ok'),
                'Native runtime authentication boundary differs: ' + path)
        result[path] = response.status
    return result


def configure(node_ip):
    # Use the runtime install/deploy lock through preflight, restart and rollback.
    lock = os.open(Path('/run/railshot-deployment.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A runtime deployment is active') from None
        return configure_locked(node_ip)
    finally:
        os.close(lock)


def configure_locked(node_ip):
    require(str(ipaddress.IPv4Address(node_ip)) == node_ip, 'Registered node IPv4 required')
    config = Path('/etc/rancher/k3s/config.yaml')
    auth = Path(AUTH_PATH)
    role = Path('/etc/railshot/node-role')
    require(not role.exists() or role.read_text().strip() == 'runtime', 'Control/API node is forbidden')
    require(not Path('/etc/rancher/k3s/config.yaml.d').exists(), 'K3s config fragments are not supported')
    require(not config.is_symlink() and not auth.is_symlink(), 'Symlinked authentication config refused')
    version = native(['k3s', '--version']).splitlines()[0]
    require('v1.34.11+k3s1 ' in version, 'Reviewed K3s version required')
    require(native(['k3s', 'kubectl', '--request-timeout=15s', 'get', '--raw=/readyz']).strip() == 'ok',
            'Existing runtime is not ready')
    before = config.read_bytes()
    after = desired_config(before)
    expected_auth = (json.dumps(AUTH, indent=2) + '\n').encode()
    previous_auth = auth.read_bytes() if auth.exists() else None
    require(previous_auth is None or previous_auth == expected_auth, 'Existing authentication file differs')
    ca = Path('/var/lib/rancher/k3s/server/tls/server-ca.crt').read_text()
    require('PRIVATE KEY' not in ca, 'Only a public CA certificate is allowed')
    workloads = workload_identity()
    state = Path('/var/lib/railshot/runtime-healthz')
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(state / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        require(config.read_bytes() == before and (auth.read_bytes() if auth.exists() else None) == previous_auth,
                'Runtime configuration changed during preflight')
        digest = hashlib.sha256(before).hexdigest()
        backup = state / ('config-' + digest + '.yaml')
        if backup.exists():
            require(backup.read_bytes() == before, 'Backup differs')
        else:
            write_atomic(backup, before)
        changed = after != before or previous_auth != expected_auth
        try:
            if changed:
                write_atomic(auth, expected_auth)
                write_atomic(config, after)
                native(['systemctl', 'restart', 'k3s'])
            deadline = time.monotonic() + 90
            while True:
                try:
                    checks = check_health(ca, node_ip)
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(2)
            require(workload_identity() == workloads, 'Existing workload identity or spec changed')
        except Exception:
            rollback_verified = False
            if changed:
                require(config.read_bytes() == after and auth.read_bytes() == expected_auth,
                        'Concurrent configuration edit prevents automatic rollback')
                write_atomic(config, before)
                if previous_auth is None:
                    auth.unlink()
                else:
                    write_atomic(auth, previous_auth)
                native(['systemctl', 'restart', 'k3s'])
                deadline = time.monotonic() + 90
                while True:
                    try:
                        require(native(['k3s', 'kubectl', '--request-timeout=15s', 'get', '--raw=/readyz']).strip() == 'ok',
                                'Restored runtime is not ready')
                        require(workload_identity() == workloads, 'Restored workload identity differs')
                        rollback_verified = True
                        break
                    except Exception:
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(2)
            write_atomic(state / 'receipt.json', (json.dumps({'status': 'failed',
                'backup': str(backup), 'config_before_sha256': digest,
                'rollback_verified': rollback_verified}, indent=2) + '\n').encode())
            raise
        receipt = {'changed': changed, 'version': version, 'config_before_sha256': digest,
                   'config_after_sha256': hashlib.sha256(after).hexdigest(), 'backup': str(backup),
                   'ca_sha256': hashlib.sha256(ca.encode()).hexdigest(), 'checks': checks,
                   'workloads_preserved': True, 'workload_count': len(workloads)}
        write_atomic(state / 'receipt.json', (json.dumps(receipt, indent=2) + '\n').encode())
        return {**receipt, 'ca_pem': ca}
    finally:
        os.close(lock)


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        require(set(request) == {'node_ip'}, 'Only the registered node identity is accepted')
        print(json.dumps(configure(request['node_ip'])))
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__, 'message': str(error)}))
        raise SystemExit(1)
