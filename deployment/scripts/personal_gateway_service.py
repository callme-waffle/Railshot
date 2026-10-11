#!/usr/bin/env python3
"""Root-only WireGuard gateway service on an authenticated local Unix socket."""
import argparse
import errno
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import select
import socket
import socketserver
import stat
import sys
import tempfile
import threading

import personal_wireguard as gateway


MAX_REQUEST = 16 * 1024
MAX_TOKEN = 513  # A 512-character token plus its conventional trailing newline.
TOKEN = re.compile(r'[A-Za-z0-9._~-]{32,512}')
STATE_DIRECTORY = Path('/var/lib/railshot-personal-gateway')
WIREGUARD_DIRECTORY = Path('/etc/wireguard')


def require(value, message='GATEWAY_SERVICE_INVALID'):
    if not value:
        raise ValueError(message)


def private_token(path):
    path = Path(path)
    info = path.stat()
    require(path.is_absolute() and stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and not info.st_mode & 0o007 and info.st_size <= MAX_TOKEN)
    value = path.read_text().strip()
    require(TOKEN.fullmatch(value))
    return value


def atomic_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.gateway-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def normalize_private_directory(directory, owner_uid=0):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    # kubelet may initially apply fsGroup write bits to a new PVC root. Only
    # normalize a real expected-owner mount; never adopt a symlink or foreign owner.
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == owner_uid)
    directory.chmod(0o700)
    info = directory.lstat()
    require(stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == owner_uid)
    return directory


def initialize(config_path, key_source, endpoint):
    """Materialize immutable operator inputs into root-owned persistent files."""
    require(os.geteuid() == 0, 'GATEWAY_ROOT_REQUIRED')
    require(re.fullmatch(r'[A-Za-z0-9.-]+:[0-9]{1,5}', endpoint or ''))
    state = STATE_DIRECTORY
    wireguard = WIREGUARD_DIRECTORY
    for directory in (state, wireguard):
        normalize_private_directory(directory)
    source = Path(key_source)
    source_info = source.stat()
    # Kubernetes may add fsGroup read permission to a projected Secret. The API
    # never mounts this key volume; copy it once into root-only persistent state.
    require(source.is_absolute() and stat.S_ISREG(source_info.st_mode) and source_info.st_uid == 0
            and not source_info.st_mode & 0o037 and source_info.st_size <= 128)
    secret = source.read_text().strip()
    gateway.key(secret)
    key_path = state / 'private.key'
    if key_path.exists():
        require(hmac.compare_digest(gateway.private(key_path).strip(), secret), 'GATEWAY_KEY_CHANGED')
    else:
        atomic_private(key_path, secret + '\n')
    config = {'version': 1, 'lifecycle': 'direct', 'interface': 'railshotwg',
              'address_pool': '10.253.240.0/24', 'server_address': '10.253.240.1/32',
              'endpoint': endpoint, 'listen_port': 51820, 'private_key_file': str(key_path),
              'ledger_path': str(state / 'peers.json')}
    gateway.validate(config)
    destination = Path(config_path)
    if destination.exists():
        require(json.loads(gateway.private(destination)) == config, 'GATEWAY_CONFIGURATION_CHANGED')
    else:
        atomic_private(destination, json.dumps(config, sort_keys=True) + '\n')
    return config


def peer_uid(connection):
    if not hasattr(socket, 'SO_PEERCRED'):
        raise ValueError('GATEWAY_PEER_CREDENTIALS_UNAVAILABLE')
    import struct
    _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return uid


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, path, handler, *, config, token, client_uid):
        self.gateway_config = config
        self.gateway_token = token
        self.gateway_client_uid = client_uid
        super().__init__(path, handler)


class TCPHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Private service endpoint; Kubernetes NetworkPolicy restricts callers."""
    daemon_threads = True
    block_on_close = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, config, token):
        self.gateway_config = config
        self.gateway_token = token
        self.gateway_client_uid = None
        self.stream_slots = threading.BoundedSemaphore(64)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.stream_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.stream_slots.release()
            raise

    def get_request(self):
        connection, address = super().get_request()
        # Bound unauthenticated connections that stall before HTTP headers.
        connection.settimeout(10)
        return connection, address

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.stream_slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'railshot-personal-gateway/1'
    sys_version = ''

    def log_message(self, *_):
        return

    def answer(self, status, value):
        body = json.dumps(value, separators=(',', ':')).encode()
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # A probe may be terminated by the container runtime while the
            # response is in flight. Never turn that into a server traceback.
            return

    def authenticated(self, *, mutation=False):
        authorization = self.headers.get('Authorization', '')
        supplied = authorization[7:] if authorization.startswith('Bearer ') else ''
        if not hmac.compare_digest(supplied, self.server.gateway_token):
            self.answer(401, {'status': 'failed', 'error': 'GATEWAY_UNAUTHENTICATED'})
            return False
        if mutation and self.server.gateway_client_uid is not None \
                and peer_uid(self.connection) != self.server.gateway_client_uid:
            self.answer(403, {'status': 'failed', 'error': 'GATEWAY_CLIENT_REJECTED'})
            return False
        return True

    def do_GET(self):
        if self.path != '/healthz':
            self.answer(404, {'status': 'failed', 'error': 'GATEWAY_ROUTE_NOT_FOUND'})
            return
        if not self.authenticated():
            return
        try:
            gateway.validate(self.server.gateway_config)
            with gateway.locked_ledger(self.server.gateway_config) as ledger:
                gateway.verify_applied(self.server.gateway_config, ledger, gateway.run)
            self.answer(200, {'status': 'ready'})
        except Exception:
            self.answer(503, {'status': 'failed', 'error': 'GATEWAY_NOT_READY'})

    def do_POST(self):
        if self.path != '/requests':
            self.answer(404, {'status': 'failed', 'error': 'GATEWAY_ROUTE_NOT_FOUND'})
            return
        if not self.authenticated(mutation=True):
            return
        if self.headers.get('Content-Type') != 'application/json':
            self.answer(415, {'status': 'failed', 'error': 'GATEWAY_REQUEST_INVALID'})
            return
        try:
            length = int(self.headers.get('Content-Length', ''))
            require(0 < length <= MAX_REQUEST, 'GATEWAY_REQUEST_INVALID')
            raw = self.rfile.read(length)
            require(len(raw) == length, 'GATEWAY_REQUEST_INVALID')
            request = json.loads(raw)
            result = gateway.execute(self.server.gateway_config, request)
            self.answer(200, result)
        except Exception:
            self.answer(422, {'status': 'failed', 'error': 'GATEWAY_OPERATION_FAILED'})

    def do_CONNECT(self):
        """Bridge only a registered peer's management SSH socket, without parsing SSH."""
        if not self.authenticated():
            return
        match = re.fullmatch(r'/streams/([A-Za-z0-9_-]{1,128})/([1-9][0-9]*)', self.path)
        if not match or self.headers.get('Content-Length') or self.headers.get('Transfer-Encoding'):
            self.answer(400, {'status': 'failed', 'error': 'GATEWAY_STREAM_INVALID'})
            return
        target_id, generation = match.groups()
        try:
            with gateway.locked_ledger(self.server.gateway_config) as ledger:
                peer = ledger.get(f'{target_id}:{generation}')
                require(peer and not peer['removed'], 'GATEWAY_PEER_UNKNOWN')
                address = str(gateway.ipaddress.ip_interface(peer['address']).ip)
                allowed = gateway.run(['wg', 'show', self.server.gateway_config['interface'], 'allowed-ips'])
                require(any(row.split() == [peer['public_key'], peer['address']]
                            for row in allowed.splitlines()), 'GATEWAY_PEER_NOT_APPLIED')
            upstream = socket.create_connection((address, 2222), timeout=5)
        except ValueError:
            self.answer(404, {'status': 'failed', 'error': 'GATEWAY_PEER_UNKNOWN'})
            return
        except (OSError, TimeoutError):
            self.answer(502, {'status': 'failed', 'error': 'GATEWAY_PEER_UNREACHABLE'})
            return
        with upstream:
            self.send_response(200, 'Connection Established')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.flush()
            self.connection.settimeout(None)
            upstream.settimeout(None)
            self.close_connection = True
            sockets = (self.connection, upstream)
            # A finite stream lifetime prevents abandoned API connections from
            # retaining an approved route indefinitely after target removal.
            import time
            deadline = time.monotonic() + 300
            next_peer_check = time.monotonic() + 5
            try:
                while time.monotonic() < deadline:
                    wait = max(0, min(next_peer_check, deadline) - time.monotonic())
                    readable, _, _ = select.select(sockets, [], [], wait)
                    if time.monotonic() >= next_peer_check:
                        with gateway.locked_ledger(self.server.gateway_config) as ledger:
                            current = ledger.get(f'{target_id}:{generation}')
                            if not current or current['removed'] or current['public_key'] != peer['public_key']:
                                return
                        next_peer_check = time.monotonic() + 5
                    for source in readable:
                        chunk = source.recv(65536)
                        if not chunk:
                            return
                        (upstream if source is self.connection else self.connection).sendall(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                return


def acquire_instance_lock(path, owner_uid=None):
    path = Path(path)
    owner_uid = os.geteuid() if owner_uid is None else owner_uid
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == owner_uid
                and stat.S_IMODE(info.st_mode) == 0o600, 'GATEWAY_LOCK_UNSAFE')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('GATEWAY_ALREADY_RUNNING') from None
        return fd
    except Exception:
        os.close(fd)
        raise


def remove_stale_socket(path, client_gid, owner_uid=None):
    """Remove only an expected, inactive socket left by this root service."""
    path = Path(path)
    owner_uid = os.geteuid() if owner_uid is None else owner_uid
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    require(stat.S_ISSOCK(info.st_mode) and info.st_uid == owner_uid and info.st_gid == client_gid
            and stat.S_IMODE(info.st_mode) == 0o660, 'GATEWAY_SOCKET_UNSAFE')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1)
        try:
            client.connect(str(path))
        except OSError as error:
            if error.errno == errno.ENOENT:
                return False
            require(error.errno == errno.ECONNREFUSED, 'GATEWAY_SOCKET_ACTIVE')
        else:
            raise ValueError('GATEWAY_ALREADY_RUNNING')
    # Refuse replacement races: only unlink the exact inode inspected above.
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return False
    require((observed.st_dev, observed.st_ino) == (info.st_dev, info.st_ino)
            and stat.S_ISSOCK(observed.st_mode) and observed.st_uid == owner_uid
            and observed.st_gid == client_gid and stat.S_IMODE(observed.st_mode) == 0o660,
            'GATEWAY_SOCKET_CHANGED')
    path.unlink()
    return True


def serve(socket_path, config_path, token_path, client_uid):
    require(os.geteuid() == 0, 'GATEWAY_ROOT_REQUIRED')
    path = Path(socket_path)
    parent = path.parent
    parent_info = parent.lstat()
    require(path.is_absolute() and stat.S_ISDIR(parent_info.st_mode)
            and parent_info.st_uid == 0 and parent_info.st_gid == client_uid
            and not parent_info.st_mode & 0o027)
    config = json.loads(gateway.private(config_path))
    gateway.validate(config)
    token = private_token(token_path)
    lock = acquire_instance_lock(parent / 'gateway.lock')
    try:
        remove_stale_socket(path, client_uid)
        with UnixHTTPServer(str(path), Handler, config=config, token=token, client_uid=client_uid) as server:
            info = path.lstat()
            require(stat.S_ISSOCK(info.st_mode) and info.st_uid == 0 and info.st_gid == client_uid,
                    'GATEWAY_SOCKET_OWNERSHIP_INVALID')
            os.chmod(path, 0o660)
            try:
                server.serve_forever(poll_interval=0.5)
            finally:
                current = path.lstat() if path.exists() else None
                if current and stat.S_ISSOCK(current.st_mode) and current.st_uid == 0 \
                        and current.st_gid == client_uid:
                    path.unlink()
    finally:
        os.close(lock)


def serve_tcp(host, port, config_path, token_path):
    require(os.geteuid() == 0, 'GATEWAY_ROOT_REQUIRED')
    config = json.loads(gateway.private(config_path))
    gateway.validate(config)
    address = gateway.ipaddress.ip_address(host)
    require(address.version == 4 and address not in gateway.ipaddress.ip_network(config['address_pool'])
            and address != gateway.ipaddress.ip_address('0.0.0.0') and 1 <= port <= 65535,
            'GATEWAY_LISTEN_INVALID')
    token = private_token(token_path)
    lock = acquire_instance_lock(Path(config['ledger_path']).parent / 'gateway.lock')
    try:
        with TCPHTTPServer((host, port), Handler, config=config, token=token) as server:
            server.serve_forever(poll_interval=0.5)
    finally:
        os.close(lock)


def probe(socket_path, token_path):
    request = (f'GET /healthz HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {private_token(token_path)}'
               '\r\nConnection: close\r\n\r\n').encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(socket_path)
        client.sendall(request)
        response = bytearray()
        while len(response) <= MAX_REQUEST:
            part = client.recv(4096)
            if not part:
                break
            response.extend(part)
    require(len(response) <= MAX_REQUEST, 'GATEWAY_RESPONSE_INVALID')
    require(response.startswith(b'HTTP/1.0 200 ') or response.startswith(b'HTTP/1.1 200 '), 'GATEWAY_NOT_READY')


def probe_tcp(host, port, token_path):
    require(gateway.ipaddress.ip_address(host).version == 4 and 1 <= port <= 65535, 'GATEWAY_PROBE_INVALID')
    request = (f'GET /healthz HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {private_token(token_path)}'
               '\r\nConnection: close\r\n\r\n').encode()
    with socket.create_connection((host, port), timeout=5) as client:
        client.sendall(request)
        response = client.recv(256)
    require(response.startswith(b'HTTP/1.0 200 ') or response.startswith(b'HTTP/1.1 200 '), 'GATEWAY_NOT_READY')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', default='/run/railshot-personal-gateway/gateway.sock')
    parser.add_argument('--config', default='/var/lib/railshot-personal-gateway/config.json')
    parser.add_argument('--token-file', default='/run/secrets/personal-gateway-ipc/ipc-token')
    parser.add_argument('--client-uid', type=int, default=1000)
    parser.add_argument('--listen-host', default=os.environ.get('PERSONAL_GATEWAY_RPC_BIND'),
                        help='Use private TCP service instead of the legacy Unix socket')
    parser.add_argument('--listen-port', type=int, default=4184)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--serve', action='store_true')
    mode.add_argument('--probe', action='store_true')
    mode.add_argument('--initialize', action='store_true')
    parser.add_argument('--key-file', default='/run/secrets/personal-gateway-key/private-key')
    parser.add_argument('--endpoint')
    args = parser.parse_args()
    try:
        if args.initialize:
            initialize(args.config, args.key_file, args.endpoint)
        elif args.probe:
            if args.listen_host:
                probe_tcp(args.listen_host, args.listen_port, args.token_file)
            else:
                probe(args.socket, args.token_file)
        else:
            if args.listen_host:
                serve_tcp(args.listen_host, args.listen_port, args.config, args.token_file)
            else:
                serve(args.socket, args.config, args.token_file, args.client_uid)
        return 0
    except Exception:
        print('Personal gateway service refused invalid private state or local IPC configuration.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
