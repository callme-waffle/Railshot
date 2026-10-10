import os
import socket

import pytest


@pytest.fixture(autouse=True)
def isolate_environment_and_network(monkeypatch):
    """Tests may only open loopback sockets, never accidentally contact a cloud."""
    for name in os.environ:
        if name.startswith("CP_"):
            monkeypatch.delenv(name, raising=False)
    original = socket.socket.connect

    def loopback_only(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError("External networking is forbidden in this test suite")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
