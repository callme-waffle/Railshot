import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from control_plane.config import Settings
from control_plane.domain.errors import UpstreamTimeout
from control_plane.main import create_app
from tests.fakes.providers import FakeCloud


def settings(**overrides):
    return Settings(api_token="test-token-only", project_id="test-project", **overrides)


def body(name="web-01"):
    return dict(
        name=name, image_id="image-test", flavor_id="flavor-test", network_ids=["network-test"]
    )


HEADERS = {"Authorization": "Bearer test-token-only"}


def test_three_servers_and_full_lifecycle():
    cloud = FakeCloud()
    with TestClient(create_app(settings(), cloud.bundle())) as client:
        ids = []
        for i in range(3):
            response = client.post("/api/v1/servers", json=body(f" web-{i} "), headers=HEADERS)
            assert response.status_code == 202
            result = response.json()
            assert result["status"] == "accepted"
            assert result["request_id"] == response.headers["x-request-id"]
            ids.append(result["resource_id"])
            assert response.headers["location"] == f"/api/v1/servers/{ids[-1]}"
        first = client.get("/api/v1/servers?limit=2", headers=HEADERS).json()
        second = client.get(
            "/api/v1/servers", params={"limit": 2, "marker": first["next_marker"]}, headers=HEADERS
        ).json()
        assert len(first["items"]) == 2 and len(second["items"]) == 1
        assert {row["id"] for row in first["items"] + second["items"]} == set(ids)
        assert second["next_marker"] is None
        for server_id in ids:
            location = f"/api/v1/servers/{server_id}"
            for action, expected in [
                ("stop", "SHUTOFF"),
                ("start", "ACTIVE"),
                ("reboot", "ACTIVE"),
            ]:
                assert (
                    client.post(
                        location + "/actions", json={"action": action}, headers=HEADERS
                    ).status_code
                    == 202
                )
                assert client.get(location, headers=HEADERS).json()["status"] == expected
            assert client.delete(location, headers=HEADERS).status_code == 202
            assert client.get(location, headers=HEADERS).status_code == 404
            response = client.delete(location, headers=HEADERS)
            assert response.status_code == 204 and not response.content
        assert not cloud.servers


def test_auth_precedes_json_parsing_and_never_calls_provider():
    cloud = FakeCloud()
    with TestClient(create_app(settings(), cloud.bundle())) as client:
        response = client.post("/api/v1/servers", content="{invalid json")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["x-request-id"] == response.json()["error"]["request_id"]
        assert cloud.calls == []
        assert client.get("/health/live").status_code == 200
        assert cloud.calls == []
        duplicate = [("Authorization", "Bearer test-token-only"), ("Authorization", "Bearer other")]
        assert client.get("/api/v1/servers", headers=duplicate).status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "other"},
        {"network_ids": []},
        {"image_id": "missing"},
        {"network_ids": ["network-test"] * 2},
    ],
)
def test_invalid_create_never_submits(change):
    cloud = FakeCloud()
    with TestClient(create_app(settings(), cloud.bundle())) as client:
        response = client.post("/api/v1/servers", json=body() | change, headers=HEADERS)
        assert response.status_code == 422
        assert "create_server" not in cloud.calls


def test_timeout_is_not_retried_or_leaked(monkeypatch):
    cloud = FakeCloud()
    calls = []

    def timeout(ctx, spec):
        calls.append(spec)
        raise UpstreamTimeout(outcome_unknown=True) from RuntimeError("super-secret-provider-body")

    monkeypatch.setattr(cloud, "create_server", timeout)
    with TestClient(create_app(settings(), cloud.bundle())) as client:
        response = client.post("/api/v1/servers", json=body(), headers=HEADERS)
        assert response.status_code == 504
        assert response.json()["error"]["outcome_unknown"] is True
        assert response.json()["error"]["retryable"] is False
        assert "super-secret" not in response.text
        assert len(calls) == 1


def test_ready_and_internal_failure(monkeypatch, caplog):
    cloud = FakeCloud()
    with TestClient(create_app(settings(), cloud.bundle())) as client:
        assert client.get("/health/ready", headers=HEADERS).status_code == 200
        cloud.ready = False
        assert client.get("/health/ready", headers=HEADERS).status_code == 503

        def broken(ctx, page):
            raise RuntimeError("private credential value")

        monkeypatch.setattr(cloud, "list_servers", broken)
        response = client.get("/api/v1/servers", headers=HEADERS)
        assert response.status_code == 500
        assert response.headers["x-request-id"] == response.json()["error"]["request_id"]
        assert "private credential value" not in response.text + caplog.text


def test_real_loopback_http_calls():
    """Real HTTP transport + Uvicorn + fake provider, not a live OpenStack test."""
    cloud = FakeCloud()
    app = create_app(settings(), cloud.bundle())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Uvicorn did not start"
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", headers=HEADERS, timeout=5, trust_env=False
        ) as client:
            assert client.get("/health/live").status_code == 200
            assert client.get("/health/ready").status_code == 200
            for kind in ("images", "flavors", "networks"):
                assert len(client.get(f"/api/v1/{kind}").json()["items"]) == 1
            response = client.post("/api/v1/servers", json=body())
            assert response.status_code == 202
            location = response.headers["location"]
            assert client.get(location).json()["status"] == "ACTIVE"
            assert client.post(location + "/actions", json={"action": "stop"}).status_code == 202
            assert client.get(location).json()["status"] == "SHUTOFF"
            assert client.delete(location).status_code == 202
            assert client.get(location).status_code == 404
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive(), "Uvicorn did not shut down"
