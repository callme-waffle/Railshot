from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from control_plane.api.error_handlers import register_error_handlers
from control_plane.api.router import router
from control_plane.bootstrap import (
    get_catalog_service,
    get_compute_service,
    get_context,
    get_health_service,
)
from control_plane.domain.errors import (
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    QuotaExceeded,
    RateLimited,
    Unauthenticated,
    UpstreamFailure,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from control_plane.domain.models import (
    Accepted,
    Address,
    DeleteResult,
    Flavor,
    Image,
    Network,
    Page,
    RequestContext,
    Server,
)

CONTEXT = RequestContext("request-test", "operator", "project", "provider")
SERVER = Server("s1", "web", "project", "ACTIVE", (Address("net", "10.0.0.2", 4),))
BODY = {"name": "web", "image_id": "image", "flavor_id": "small", "network_ids": ["net"]}


@pytest.fixture
def http():
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router)
    compute, catalog, health = Mock(), Mock(), Mock()
    compute.create_server.return_value = Accepted("s1", "create")
    compute.get_server.return_value = SERVER
    compute.list_servers.return_value = Page((SERVER,), "s1")
    compute.delete_server.return_value = DeleteResult("s1", False)
    compute.act_server.return_value = Accepted("s1", "start")
    catalog.list_images.return_value = Page((Image("img", "ubuntu", "active"),), None)
    catalog.list_flavors.return_value = Page((Flavor("small", "small", 1, 1024, 10),), None)
    catalog.list_networks.return_value = Page((Network("net", "private", "ACTIVE", False),), None)
    health.check_ready.return_value = True
    app.dependency_overrides[get_context] = lambda: CONTEXT
    app.dependency_overrides[get_compute_service] = lambda: compute
    app.dependency_overrides[get_catalog_service] = lambda: catalog
    app.dependency_overrides[get_health_service] = lambda: health

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = CONTEXT.request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = CONTEXT.request_id
        return response

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, compute, catalog, health


def test_create_converts_list_to_tuple_and_returns_acceptance(http):
    client, compute, _, _ = http
    response = client.post("/api/v1/servers", json=BODY)
    assert response.status_code == 202
    assert response.headers["location"] == "/api/v1/servers/s1"
    assert response.headers["x-request-id"] == CONTEXT.request_id
    assert response.json() == {
        "resource_id": "s1",
        "action": "create",
        "status": "accepted",
        "request_id": CONTEXT.request_id,
    }
    ctx, spec = compute.create_server.call_args.args
    assert ctx == CONTEXT
    assert spec.network_ids == ("net",)


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "project_id": "other-project"},
        {**BODY, "network_ids": []},
        {**BODY, "flavor_id": 123},
        {**BODY, "name": "   "},
        {**BODY, "image_id": " "},
        {"password": "sensitive-value"},
    ],
)
def test_invalid_body_never_calls_service_and_does_not_echo_inputs(http, body):
    client, compute, _, _ = http
    response = client.post("/api/v1/servers", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert "sensitive-value" not in response.text
    assert "other-project" not in response.text
    compute.create_server.assert_not_called()


@pytest.mark.parametrize(
    "suffix",
    [
        "?limit=0",
        "?limit=101",
        "?limit=no",
        "?marker=",
        "?marker=%20",
        "?limit=1&limit=2",
        "?marker=a&marker=b",
        "?unknown=sensitive-value",
    ],
)
def test_invalid_page_rejected_before_service(http, suffix):
    client, compute, _, _ = http
    response = client.get("/api/v1/servers" + suffix)
    assert response.status_code == 422
    compute.list_servers.assert_not_called()
    assert "sensitive-value" not in response.text


def test_pagination_and_resource_serialization(http):
    client, compute, _, _ = http
    response = client.get("/api/v1/servers?limit=3&marker=before")
    assert response.status_code == 200
    assert response.json()["next_marker"] == "s1"
    assert response.json()["items"][0]["addresses"][0]["address"] == "10.0.0.2"
    ctx, page = compute.list_servers.call_args.args
    assert ctx == CONTEXT and page.limit == 3 and page.marker == "before"
    assert client.get("/api/v1/servers/s1").json()["id"] == "s1"


@pytest.mark.parametrize("kind,id", [("images", "img"), ("flavors", "small"), ("networks", "net")])
def test_catalog_routes(http, kind, id):
    client, _, catalog, _ = http
    response = client.get(f"/api/v1/{kind}")
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == id
    assert response.json()["next_marker"] is None
    assert getattr(catalog, "list_" + kind).call_args.args[1].limit == 20


def test_delete_distinguishes_absence_and_acceptance(http):
    client, compute, _, _ = http
    response = client.delete("/api/v1/servers/s1")
    assert response.status_code == 202
    assert response.json()["action"] == "delete"
    assert response.headers["location"] == "/api/v1/servers/s1"
    compute.delete_server.return_value = DeleteResult("s1", True)
    response = client.delete("/api/v1/servers/s1")
    assert response.status_code == 204 and response.content == b""
    assert "location" not in response.headers


@pytest.mark.parametrize("action", ["start", "stop", "reboot"])
def test_actions(http, action):
    client, compute, _, _ = http
    compute.act_server.return_value = Accepted("s1", action)
    response = client.post("/api/v1/servers/s1/actions", json={"action": action})
    assert response.status_code == 202
    assert response.json()["action"] == action
    compute.act_server.assert_called_once_with(CONTEXT, "s1", action)


@pytest.mark.parametrize("body", [{"action": "hard_reboot"}, {"action": "start", "force": True}])
def test_unsupported_actions(http, body):
    client, compute, _, _ = http
    assert client.post("/api/v1/servers/s1/actions", json=body).status_code == 422
    compute.act_server.assert_not_called()


@pytest.mark.parametrize(
    "error,status",
    [
        (InvalidInput(), 422),
        (Unauthenticated(), 401),
        (Forbidden(), 403),
        (NotFound(), 404),
        (Conflict(), 409),
        (QuotaExceeded(), 409),
        (RateLimited(), 429),
        (UpstreamFailure(), 502),
        (UpstreamUnavailable(), 503),
        (UpstreamTimeout(outcome_unknown=True), 504),
    ],
)
def test_domain_error_envelopes(http, error, status):
    client, compute, _, _ = http
    compute.get_server.side_effect = error
    response = client.get("/api/v1/servers/s1")
    assert response.status_code == status
    assert response.json()["error"] == {
        "code": error.code,
        "message": error.message,
        "request_id": CONTEXT.request_id,
        "retryable": error.retryable,
        "outcome_unknown": error.outcome_unknown,
    }
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"


def test_routing_errors_share_envelope_and_keep_allow(http):
    client, _, _, _ = http
    assert client.get("/not-found").json()["error"]["code"] == "NOT_FOUND"
    response = client.put("/api/v1/servers/s1")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert "GET" in response.headers["allow"]


def test_health_and_internal_failure(http):
    client, compute, _, health = http
    assert client.get("/health/live").json() == {"status": "ok"}
    health.check_ready.assert_not_called()
    assert client.get("/health/ready").status_code == 200
    health.check_ready.return_value = False
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    compute.get_server.side_effect = RuntimeError("secret-provider-password")
    response = client.get("/api/v1/servers/s1")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret-provider-password" not in response.text
