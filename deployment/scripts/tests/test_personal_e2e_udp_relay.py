import importlib.util
from pathlib import Path
import socket
import struct
import threading

import pytest


SPEC = importlib.util.spec_from_file_location('personal_e2e_udp_relay', Path(__file__).resolve().parents[1] / 'personal-e2e-udp-relay.py')
udp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(udp)


def test_frames_preserve_datagram_boundaries_across_fragmented_tcp_and_reject_invalid_lengths():
    frames = udp.Frames()
    data = udp.frame(b'one') + udp.frame(b'two')
    assert frames.read(data[:1]) == []
    assert frames.read(data[1:4]) == []
    assert frames.read(data[4:]) == [b'one', b'two']
    for length in (0, udp.MAX_DATAGRAM + 1, 65535):
        with pytest.raises(ValueError):
            udp.Frames().read(struct.pack('!H', length))
    with pytest.raises(ValueError):
        udp.loopback('0.0.0.0')
    with pytest.raises(ValueError):
        udp.loopback('10.0.2.67')


def test_fixed_destination_relay_carries_real_datagrams_and_filters_other_sources():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as destination, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as attacker, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connected:
        destination.bind(('127.0.0.1', 0))
        connected.connect(destination.getsockname())
        client, server = socket.socketpair()
        errors = []

        def run():
            try:
                udp.relay(server, connected, destination.getsockname())
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            client.settimeout(2)
            destination.settimeout(2)
            client.sendall(udp.frame(b'probe'))
            message, source = destination.recvfrom(4096)
            assert message == b'probe'
            attacker.sendto(b'injected', source)
            destination.sendto(b'accepted', source)
            observed = udp.Frames()
            replies = []
            while not replies:
                replies.extend(observed.read(client.recv(4096)))
            assert replies == [b'accepted']
        finally:
            client.close()
            worker.join(timeout=2)
            server.close()
        assert not worker.is_alive()
        assert not errors
