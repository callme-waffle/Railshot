import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from contextlib import nullcontext
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location('personal_gateway_service', SCRIPTS / 'personal_gateway_service.py')
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)


def request(path, raw):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(path)
        client.sendall(raw)
        response = b''
        while True:
            part = client.recv(4096)
            if not part:
                return response
            response += part


def response(path, method, route, token, body=b''):
    raw = (f'{method} {route} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n'
           f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body
    reply = request(path, raw)
    head, content = reply.split(b'\r\n\r\n', 1)
    return int(head.split()[1]), json.loads(content)


def test_token_accepts_contract_boundaries_and_rejects_permissions():
    with tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as temporary:
        path = Path(temporary) / 'token'
        for token in ('a' * 32, '._~-' * 128):
            path.write_text(token + '\n')
            path.chmod(0o600)
            assert service.private_token(path) == token
        path.write_text('a' * 31)
        try:
            service.private_token(path)
        except ValueError:
            pass
        else:
            raise AssertionError('short token accepted')
        path.write_text('a' * 32)
        path.chmod(0o604)
        try:
            service.private_token(path)
        except ValueError:
            pass
        else:
            raise AssertionError('public token file accepted')


def test_authenticated_socket_forwards_only_bounded_requests_and_health_is_side_effect_free():
    token = 'token._~-value-' + 'a' * 32
    config = {'version': 1}
    with tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as temporary:
        path = str(Path(temporary) / 'gateway.sock')
        server = service.UnixHTTPServer(path, service.Handler, config=config, token=token, client_uid=os.getuid())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        result = {'status': 'succeeded', 'reachable': False, 'tunnel': {'address': '10.253.240.2/32'}}
        body = json.dumps({'action': 'register', 'target_id': 'target_1', 'generation': 1,
                           'public_key': 'A' * 43 + '='}).encode()
        try:
            with patch.object(service, 'peer_uid', return_value=os.getuid()), \
                    patch.object(service.gateway, 'execute', return_value=result) as execute, \
                    patch.object(service.gateway, 'validate'), \
                    patch.object(service.gateway, 'locked_ledger', return_value=nullcontext({})) as locked, \
                    patch.object(service.gateway, 'verify_applied') as applied:
                assert response(path, 'POST', '/requests', 'wrong-' + token, body)[0] == 401
                status, value = response(path, 'POST', '/requests', token, body)
                assert status == 200 and value == result
                execute.assert_called_once_with(config, json.loads(body))
                status, value = response(path, 'GET', '/healthz', token)
                assert status == 200 and value == {'status': 'ready'}
                locked.assert_called_once_with(config)
                applied.assert_called_once_with(config, {}, service.gateway.run)
                execute.assert_called_once()
                token_file = Path(temporary) / 'ipc-token'
                token_file.write_text(token + '\n')
                token_file.chmod(0o600)
                service.probe(path, token_file)
                applied.side_effect = ValueError('GATEWAY_INTERFACE_MISSING')
                status, value = response(path, 'GET', '/healthz', token)
                assert status == 503 and value['error'] == 'GATEWAY_NOT_READY'
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


def test_response_disconnect_during_headers_does_not_escape_handler():
    handler = object.__new__(service.Handler)
    handler.send_response = lambda _status: None
    handler.send_header = lambda *_args: None

    def disconnected():
        raise BrokenPipeError

    handler.end_headers = disconnected
    handler.answer(200, {'status': 'ready'})


def test_stale_socket_recovery_refuses_live_and_unsafe_paths():
    with tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as temporary:
        root = Path(temporary)
        stale = root / 'stale.sock'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as abandoned:
            abandoned.bind(str(stale))
        stale.chmod(0o660)
        assert service.remove_stale_socket(stale, stale.lstat().st_gid) is True
        assert not stale.exists()

        live = root / 'live.sock'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(live))
            listener.listen(1)
            live.chmod(0o660)
            try:
                service.remove_stale_socket(live, live.lstat().st_gid)
            except ValueError as error:
                assert str(error) == 'GATEWAY_ALREADY_RUNNING'
            else:
                raise AssertionError('live gateway socket was removed')
            assert live.exists()

        unsafe = root / 'gateway.sock'
        unsafe.write_text('not a socket')
        unsafe.chmod(0o660)
        try:
            service.remove_stale_socket(unsafe, unsafe.lstat().st_gid)
        except ValueError as error:
            assert str(error) == 'GATEWAY_SOCKET_UNSAFE'
        else:
            raise AssertionError('regular file was removed as a stale socket')
        assert unsafe.is_file()

        target = root / 'foreign-target'
        target.write_text('keep')
        link = root / 'linked.sock'
        link.symlink_to(target)
        try:
            service.remove_stale_socket(link, target.lstat().st_gid)
        except ValueError as error:
            assert str(error) == 'GATEWAY_SOCKET_UNSAFE'
        else:
            raise AssertionError('symlink was followed as a stale socket')
        assert link.is_symlink() and target.read_text() == 'keep'


def test_instance_lock_rejects_a_second_server_and_is_reusable_after_close():
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / 'gateway.lock'
        first = service.acquire_instance_lock(path)
        try:
            try:
                service.acquire_instance_lock(path)
            except ValueError as error:
                assert str(error) == 'GATEWAY_ALREADY_RUNNING'
            else:
                raise AssertionError('second gateway acquired the instance lock')
        finally:
            os.close(first)
        second = service.acquire_instance_lock(path)
        os.close(second)


def test_initialize_normalizes_new_fsgroup_pvc_before_writing_private_state():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        state, wireguard = root / 'state', root / 'wireguard'
        for directory in (state, wireguard):
            directory.mkdir()
            directory.chmod(0o2770)
            service.normalize_private_directory(directory, os.geteuid())
            assert (directory.stat().st_mode & 0o7777) == 0o700
        if os.geteuid() != 0:
            return
        key = root / 'private-key'
        key.write_text('A' * 43 + '=\n')
        key.chmod(0o440)
        with patch.object(service, 'STATE_DIRECTORY', state), \
                patch.object(service, 'WIREGUARD_DIRECTORY', wireguard), \
                patch.object(service.gateway, 'key'), patch.object(service.gateway, 'validate'):
            config = service.initialize(state / 'config.json', key, 'gateway.example:51820')
        assert config['private_key_file'] == str(state / 'private.key')
        assert (state / 'private.key').stat().st_mode & 0o777 == 0o600
