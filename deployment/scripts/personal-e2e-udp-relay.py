#!/usr/bin/env python3
"""TEST ONLY: one WireGuard peer's UDP datagrams over an existing SSH TCP forward.

This checks encrypted tunnel behavior when direct UDP is unavailable. It is not
production networking and does not satisfy public/native UDP acceptance.
All listeners and the single fixed destination are IPv4 loopback only.
"""
import argparse
import ipaddress
import selectors
import socket
import struct

MAX_DATAGRAM = 4096


def loopback(value):
    address = ipaddress.ip_address(value)
    if address.version != 4 or not address.is_loopback:
        raise ValueError('Only IPv4 loopback is permitted')
    return str(address)


class Frames:
    def __init__(self):
        self.pending = bytearray()

    def read(self, data):
        self.pending.extend(data)
        messages = []
        while len(self.pending) >= 2:
            length, = struct.unpack('!H', self.pending[:2])
            if not 1 <= length <= MAX_DATAGRAM:
                raise ValueError('Invalid test datagram length')
            if len(self.pending) < length + 2:
                break
            messages.append(bytes(self.pending[2:length + 2]))
            del self.pending[:length + 2]
        return messages


def frame(data):
    if not 1 <= len(data) <= MAX_DATAGRAM:
        raise ValueError('Invalid test datagram length')
    return struct.pack('!H', len(data)) + data


def relay(stream, datagrams, destination=None):
    """A connected fixed upstream or the first observed local client, never both."""
    frames = Frames()
    peer = None
    stream.settimeout(10)
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ, 'tcp')
        selector.register(datagrams, selectors.EVENT_READ, 'udp')
        while True:
            for selected, _event in selector.select():
                if selected.data == 'tcp':
                    data = stream.recv(8192)
                    if not data:
                        if frames.pending:
                            raise ValueError('Incomplete test datagram')
                        return
                    for message in frames.read(data):
                        if destination is not None:
                            datagrams.send(message)
                        elif peer is not None:
                            datagrams.sendto(message, peer)
                else:
                    if destination is not None:
                        # connect() pins both the outgoing destination and
                        # accepted source; another sender cannot inject replies.
                        message = datagrams.recv(MAX_DATAGRAM + 1)
                    else:
                        message, observed = datagrams.recvfrom(MAX_DATAGRAM + 1)
                        if not ipaddress.ip_address(observed[0]).is_loopback:
                            continue
                        if peer is None:
                            peer = observed
                        if observed != peer:
                            continue
                    stream.sendall(frame(message))


def serve_tcp(address, port, udp_address, udp_port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((loopback(address), port))
        listener.listen(1)
        while True:
            stream, _peer = listener.accept()
            with stream, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as upstream:
                destination = (loopback(udp_address), udp_port)
                upstream.connect(destination)
                try:
                    relay(stream, upstream, destination)
                except (OSError, ValueError):
                    pass


def serve_udp(address, port, tcp_address, tcp_port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind((loopback(address), port))
        # The fixed SSH reverse-forward address must already be listening.
        with socket.create_connection((loopback(tcp_address), tcp_port), timeout=10) as stream:
            relay(stream, listener)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['tcp', 'udp'])
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--destination', default='127.0.0.1')
    parser.add_argument('--destination-port', type=int, required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 1024 <= args.destination_port <= 65535:
        parser.error('test ports must be unprivileged')
    try:
        (serve_tcp if args.mode == 'tcp' else serve_udp)(
            args.bind, args.port, args.destination, args.destination_port)
    except Exception:
        parser.exit(1, 'Test UDP relay stopped; no packet contents were printed.\n')


if __name__ == '__main__':
    main()
