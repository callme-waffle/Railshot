#!/usr/bin/env python3
"""Real isolated Docker WireGuard/RPC/CONNECT/restart probe, without OpenStack."""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import time
import uuid


PEER_KEY = '''import pathlib,subprocess
key=subprocess.check_output(['wg','genkey'])
path=pathlib.Path('/tmp/probe-private.key');path.write_bytes(key);path.chmod(0o600)
print(subprocess.check_output(['wg','pubkey'],input=key).decode().strip())
'''
PEER_SERVER = '''import json,pathlib,socket,subprocess
t=json.loads(pathlib.Path('/tmp/probe-tunnel.json').read_text())
run=lambda args: subprocess.run(args,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
run(['ip','link','add','probe-wg','type','wireguard'])
run(['ip','address','add',t['address'],'dev','probe-wg'])
run(['wg','set','probe-wg','private-key','/tmp/probe-private.key','peer',t['server_public_key'],
     'endpoint',t['endpoint'],'allowed-ips',t['allowed_ips'],'persistent-keepalive','1'])
run(['ip','link','set','probe-wg','up'])
run(['ip','route','add',t['allowed_ips'],'dev','probe-wg'])
with socket.socket() as server:
 server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
 server.bind((t['address'].split('/')[0],2222));server.listen(4)
 while True:
  client,_=server.accept()
  with client:
   client.settimeout(5);message=client.recv(128);client.sendall(b'wireguard:'+message)
'''


def docker(*args, input=None):
    result = subprocess.run(['docker', *args], input=input, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=True, timeout=60)
    return result.stdout.decode().strip()


def service(project, name):
    matches = docker('ps', '-q', '--filter', f'label=com.docker.compose.project={project}',
                     '--filter', f'label=com.docker.compose.service={name}').splitlines()
    if len(matches) != 1:
        raise ValueError('Exactly one active test service required')
    return matches[0], json.loads(docker('inspect', matches[0]))[0]


def token_file(path):
    info = path.lstat()
    owner = int(os.environ.get('SUDO_UID', os.geteuid()))
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in {os.geteuid(), owner} \
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 513:
        raise ValueError('Private fixture token required')
    token = path.read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9._~-]{32,512}', token):
        raise ValueError('Invalid fixture token')
    return token


def rpc(host, token, action, target):
    connection = http.client.HTTPConnection(host, 4184, timeout=10)
    try:
        body = None if action == 'health' else json.dumps({**target, 'action': action}).encode()
        connection.request('GET' if action == 'health' else 'POST',
                           '/healthz' if action == 'health' else '/requests', body,
                           {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(16385)
        if response.status != 200 or len(raw) > 16384:
            raise ValueError('Gateway request failed')
        return json.loads(raw)
    finally:
        connection.close()


def connect(host, token, route, expected=200, echo=False):
    with socket.create_connection((host, 4184), timeout=10) as stream:
        stream.sendall((f'CONNECT {route} HTTP/1.1\r\nHost: gateway\r\n'
                        f'Authorization: Bearer {token}\r\n\r\n').encode())
        headers = bytearray()
        while not headers.endswith(b'\r\n\r\n') and len(headers) < 16384:
            value = stream.recv(1)
            if not value:
                raise ValueError('Gateway disconnected')
            headers.extend(value)
        if int(headers.split(b' ', 2)[1]) != expected:
            raise ValueError('Unexpected gateway route outcome')
        if echo:
            stream.sendall(b'connection-probe')
            if stream.recv(128) != b'wireguard:connection-probe':
                raise ValueError('WireGuard byte stream mismatch')


def wait_for(check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception:
            pass
        time.sleep(2)
    raise ValueError('Real gateway recovery timed out')


def probe(project, fixture, restart):
    if not re.fullmatch(r'railshot-onprem-e2e(?:-[a-z0-9]+)?', project):
        raise ValueError('Use an isolated onprem E2E project')
    api_id, api = service(project, 'api')
    gateway_id, gateway = service(project, 'gateway')
    namespace = lambda data: Path(f"/proc/{data['State']['Pid']}/ns/net").stat().st_ino
    if namespace(api) == namespace(gateway):
        raise ValueError('Gateway shares API network namespace')
    proof = docker('exec', api_id, 'python3', '-c',
        "import socket; print(any(name=='railshotwg' for _,name in socket.if_nameindex())); "
        "print(next(line.split()[1] for line in open('/proc/self/status') if line.startswith('CapEff:')))")
    if proof.splitlines() != ['False', '0000000000000000']:
        raise ValueError('API has a WireGuard interface or capabilities')
    host = gateway['NetworkSettings']['Networks'][f'{project}_default']['IPAddress']
    token = token_file(fixture / 'ipc-token')
    if rpc(host, token, 'health', {}) != {'status': 'ready'}:
        raise ValueError('Gateway not ready')
    peer_name = f'{project}-transport-probe-{uuid.uuid4().hex[:8]}'
    target = {'target_id': f'personal-{uuid.uuid4()}', 'generation': 1}
    registered = False
    created = False
    try:
        docker('run', '-d', '--name', peer_name, '--label', f'railshot.e2e.project={project}',
               '--network', f'{project}_default', '--cap-drop', 'ALL', '--cap-add', 'NET_ADMIN',
               '--security-opt', 'no-new-privileges', '--read-only', '--tmpfs', '/tmp:mode=1777',
               '--entrypoint', 'sleep', gateway['Config']['Image'], 'infinity')
        created = True
        target['public_key'] = docker('exec', peer_name, 'python3', '-c', PEER_KEY)
        if not re.fullmatch(r'[A-Za-z0-9+/]{43}=', target['public_key']):
            raise ValueError('Peer key generation failed')
        result = rpc(host, token, 'register', target)
        registered = True
        if result.get('status') != 'succeeded':
            raise ValueError('Gateway registration failed')
        tunnel = {**result['tunnel'], 'endpoint': f'{host}:51820'}
        docker('exec', '-i', peer_name, 'python3', '-c',
            "import pathlib,sys;p=pathlib.Path('/tmp/probe-tunnel.json');p.write_bytes(sys.stdin.buffer.read());p.chmod(0o600)",
            input=json.dumps(tunnel).encode())
        docker('exec', '-d', peer_name, 'python3', '-c', PEER_SERVER)
        wait_for(lambda: rpc(host, token, 'verify', target).get('reachable') is True)
        route = f"/streams/{target['target_id']}/1"
        connect(host, token, route, echo=True)
        connect(host, token, f"/streams/{target['target_id']}/2", 404)
        connect(host, token, route + '/22', 400)
        connect(host, 'invalid-test-bearer', route, 401)
        if restart:
            docker('restart', gateway_id)
            wait_for(lambda: rpc(host, token, 'health', {}) == {'status': 'ready'})
            # A gateway namespace restart loses ephemeral WireGuard sessions.
            # The unchanged client must reach its normal rekey interval before
            # the stable server key can accept fresh encrypted traffic again.
            wait_for(lambda: rpc(host, token, 'verify', target).get('reachable') is True, seconds=180)
            connect(host, token, route, echo=True)
        removed = rpc(host, token, 'remove', target)
        if removed.get('status') != 'succeeded':
            raise ValueError('Probe peer removal failed')
        registered = False
        connect(host, token, route, 404)
        return {'status': 'passed', 'network_namespace_separate': True, 'api_capabilities': 'none',
                'native_docker_wireguard': True, 'authenticated_rpc': True, 'restricted_connect': True,
                'gateway_restart': restart, 'probe_peer_removed': True,
                'scope': 'local_docker_transport_only'}
    finally:
        try:
            if registered:
                rpc(host, token, 'remove', target)
        finally:
            if created:
                docker('rm', '-f', peer_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='railshot-onprem-e2e')
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--restart-gateway', action='store_true',
                        help='Restart only this test project gateway; do not use during other active test jobs')
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.project, args.fixture, args.restart_gateway), sort_keys=True))
    except Exception:
        parser.exit(1, 'Isolated gateway transport probe failed; no key material was printed.\n')


if __name__ == '__main__':
    main()
