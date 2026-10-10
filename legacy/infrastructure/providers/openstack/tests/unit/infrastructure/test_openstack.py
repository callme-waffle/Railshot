from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from keystoneauth1 import exceptions as ke
from openstack import exceptions as oe
from openstack.compute.v2.flavor import Flavor as SDKFlavor
from openstack.compute.v2.server import Server as SDKServer
from openstack.image.v2.image import Image as SDKImage
from openstack.network.v2.network import Network as SDKNetwork
from requests import Response

from control_plane.config import Settings
from control_plane.domain.errors import (
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    QuotaExceeded,
    RateLimited,
    UpstreamFailure,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from control_plane.domain.models import CreateServerSpec, PageRequest, RequestContext
from control_plane.infrastructure.openstack import (
    OpenStackCatalogAdapter,
    OpenStackComputeAdapter,
    OpenStackHealthAdapter,
    open_connection,
)
from control_plane.infrastructure.openstack.connection import ControllerSession
from control_plane.infrastructure.openstack.errors import normalize

CTX = RequestContext("r", "user", "project", "provider")
SPEC = CreateServerSpec("web", "image", "flavor", ("network",))


def server(id="s", project="project", **kwargs):
    return SDKServer.existing(id=id, tenant_id=project, name="web", status="ACTIVE", **kwargs)


def http_error(status, details="", cls=oe.HttpException):
    response = Response()
    response.status_code = status
    return cls(response=response, details=details)


@pytest.fixture
def conn():
    connection = Mock()
    connection.session.get_project_id.return_value = "project"
    connection.compute.get_server.return_value = server()
    return connection


def test_sdk_server_mapping_and_scope(conn):
    conn.compute.get_server.return_value = server(
        addresses={"net": [{"addr": "10.0.0.4", "version": 4}]}
    )
    result = OpenStackComputeAdapter(conn, "project", "provider").get_server(CTX, "s")
    assert result.project_id == "project"
    assert result.addresses[0].address == "10.0.0.4"
    assert not hasattr(result, "admin_password")


def test_list_bounded_and_scoped(conn):
    seen = []

    def resources():
        for i in range(100):
            seen.append(i)
            yield server(str(i))

    conn.compute.servers.return_value = resources()
    result = OpenStackComputeAdapter(conn, "project", "provider").list_servers(
        CTX, PageRequest(2, "old")
    )
    assert [s.id for s in result.items] == ["0", "1"]
    assert result.next_marker == "1"
    assert len(seen) == 3
    conn.compute.servers.assert_called_once_with(
        details=True, project_id="project", limit=3, max_items=3, marker="old"
    )


def test_foreign_list_fails_even_lookahead(conn):
    conn.compute.servers.return_value = iter([server("a"), server("b", "other")])
    with pytest.raises(UpstreamFailure):
        OpenStackComputeAdapter(conn, "project", "provider").list_servers(CTX, PageRequest(1))


@pytest.mark.parametrize("operation", ["get", "delete", "action"])
def test_foreign_detail_and_mutations_hidden(conn, operation):
    conn.compute.get_server.return_value = server(project="other")
    adapter = OpenStackComputeAdapter(conn, "project", "provider")
    with pytest.raises(NotFound):
        if operation == "get":
            adapter.get_server(CTX, "s")
        elif operation == "delete":
            adapter.delete_server(CTX, "s")
        else:
            adapter.act_server(CTX, "s", "stop")
    conn.compute.delete_server.assert_not_called()
    conn.compute.stop_server.assert_not_called()


def test_absent_delete_is_success_but_auth_failure_is_not(conn):
    adapter = OpenStackComputeAdapter(conn, "project", "provider")
    conn.compute.get_server.side_effect = http_error(404, cls=oe.NotFoundException)
    assert adapter.delete_server(CTX, "s").already_absent
    conn.compute.get_server.side_effect = http_error(401)
    with pytest.raises(UpstreamFailure):
        adapter.delete_server(CTX, "s")


def test_create_uses_low_level_sdk_attrs(conn):
    conn.compute.create_server.return_value = SDKServer.existing(id="new", adminPass="secret")
    result = OpenStackComputeAdapter(conn, "project", "provider").create_server(CTX, SPEC)
    assert result.resource_id == "new"
    conn.compute.create_server.assert_called_once_with(
        name="web", image_id="image", flavor_id="flavor", networks=[{"uuid": "network"}]
    )


def test_mutation_missing_id_marks_unknown(conn):
    conn.compute.create_server.return_value = SDKServer.existing(adminPass="secret")
    with pytest.raises(UpstreamFailure) as caught:
        OpenStackComputeAdapter(conn, "project", "provider").create_server(CTX, SPEC)
    assert caught.value.outcome_unknown


@pytest.mark.parametrize(
    "action,method",
    [("start", "start_server"), ("stop", "stop_server"), ("reboot", "reboot_server")],
)
def test_action_sdk_shape(conn, action, method):
    result = OpenStackComputeAdapter(conn, "project", "provider").act_server(CTX, "s", action)
    assert result.action == action
    getattr(conn.compute, method).assert_called_once_with(
        "s", **({"reboot_type": "SOFT"} if action == "reboot" else {})
    )


def test_context_and_real_token_project_checked_before_calls(conn):
    adapter = OpenStackComputeAdapter(conn, "project", "provider")
    with pytest.raises(Forbidden):
        adapter.get_server(RequestContext("r", "u", "other", "provider"), "s")
    conn.session.get_project_id.assert_not_called()
    conn.session.get_project_id.return_value = "other"
    with pytest.raises(UpstreamFailure):
        adapter.get_server(CTX, "s")
    conn.compute.get_server.assert_not_called()


def test_catalog_mapping_real_sdk_resources(conn):
    adapter = OpenStackCatalogAdapter(conn, "project", "provider")
    conn.image.images.return_value = iter([SDKImage.existing(id="i", status="active")])
    conn.compute.flavors.return_value = iter(
        [SDKFlavor.existing(id="f", vcpus=2, ram=4096, disk=0)]
    )
    conn.network.networks.return_value = iter(
        [SDKNetwork.existing(id="n", shared=True, tenant_id="other")]
    )
    assert adapter.list_images(CTX, PageRequest()).items[0].name == ""
    assert adapter.list_flavors(CTX, PageRequest()).items[0].ram_mb == 4096
    assert adapter.list_networks(CTX, PageRequest()).items[0].shared


def test_missing_flavor_numeric_is_invalid_upstream(conn):
    conn.compute.flavors.return_value = iter([SDKFlavor.existing(id="f")])
    with pytest.raises(UpstreamFailure):
        OpenStackCatalogAdapter(conn, "project", "provider").list_flavors(CTX, PageRequest())


def test_references_accept_shared_resources(conn):
    conn.image.get_image.return_value = SDKImage.existing(
        id="image", status="active", owner="other"
    )
    conn.compute.get_flavor.return_value = SDKFlavor.existing(id="flavor")
    conn.network.get_network.return_value = SDKNetwork.existing(
        id="network", tenant_id="other", shared=True
    )
    OpenStackCatalogAdapter(conn, "project", "provider").validate_create_references(CTX, SPEC)
    conn.image.get_image.assert_called_once_with("image")
    conn.network.get_network.assert_called_once_with("network")


def test_missing_reference_maps_invalid_input(conn):
    conn.image.get_image.side_effect = http_error(404, cls=oe.NotFoundException)
    with pytest.raises(InvalidInput):
        OpenStackCatalogAdapter(conn, "project", "provider").validate_create_references(CTX, SPEC)


@pytest.mark.parametrize(
    "status,error_type",
    [
        (400, InvalidInput),
        (401, UpstreamFailure),
        (403, Forbidden),
        (404, NotFound),
        (409, Conflict),
        (429, RateLimited),
        (503, UpstreamUnavailable),
        (504, UpstreamTimeout),
    ],
)
def test_http_error_conversion(status, error_type):
    assert isinstance(normalize(http_error(status, "sensitive token"), mutation=False), error_type)
    assert "sensitive" not in str(normalize(http_error(status, "sensitive token"), mutation=False))


def test_explicit_quota_only():
    assert isinstance(
        normalize(http_error(403, "Quota exceeded for cores"), mutation=True), QuotaExceeded
    )
    assert isinstance(normalize(http_error(403, "Policy forbids"), mutation=True), Forbidden)


@pytest.mark.parametrize("mutation", [True, False])
def test_timeout_and_connection_flags(mutation):
    for exc in (TimeoutError("secret"), ke.ConnectFailure("secret")):
        result = normalize(exc, mutation=mutation)
        assert result.outcome_unknown is mutation
        assert result.retryable is not mutation


def test_programming_errors_are_not_swallowed(conn):
    conn.compute.get_server.side_effect = TypeError("bug")
    with pytest.raises(TypeError):
        OpenStackComputeAdapter(conn, "project", "provider").get_server(CTX, "s")


def test_ready_reads_services_with_short_timeouts(conn):
    session = ControllerSession(connect_timeout=5, read_timeout=20, ready_timeout=2)
    session.get_project_id = Mock(return_value="project")
    conn.session = session
    conn.compute.servers.side_effect = lambda **kwargs: (assert_timeout(session),)
    conn.image.images.return_value = iter(())
    conn.network.networks.return_value = iter(())
    assert OpenStackHealthAdapter(conn, "project", "provider").check_ready(CTX)
    assert session.request_timeout == (5, 20)
    conn.image.images.assert_called_once_with(limit=1, max_items=1)
    session.session.close()


def assert_timeout(session):
    assert session.request_timeout == (2, 2)
    return SimpleNamespace()


def test_request_enforces_no_mutation_replays():
    session = ControllerSession(connect_timeout=5, read_timeout=20, ready_timeout=2)
    response = Response()
    response.status_code = 202
    with patch("keystoneauth1.session.Session.request", return_value=response) as request:
        assert (
            session.request(
                "https://cloud",
                "POST",
                {"server": {}},
                allow_reauth=True,
                connect_retries=8,
                status_code_retries=8,
            )
            is response
        )
        kwargs = request.call_args.kwargs
        assert kwargs["json"] == {"server": {}}
        assert kwargs["allow_reauth"] is False
        assert kwargs["redirect"] is False
        assert kwargs["connect_retries"] == kwargs["status_code_retries"] == 0
        assert kwargs["timeout"] == (5, 20)
        assert kwargs["log"] is False
    session.session.close()


def test_connection_is_explicit_lazy_scoped_and_closes(monkeypatch):
    monkeypatch.setenv("OS_CLOUD", "nonexistent-ambient-cloud")
    settings = Settings(
        api_token="token",
        project_id="project",
        auth_url="https://keystone/v3",
        username="user",
        password="pass",
        user_domain_name="Default",
    )
    with patch(
        "keystoneauth1.session.Session.get_project_id", side_effect=AssertionError("unexpected I/O")
    ):
        with open_connection(settings) as connection:
            session = connection.session
            assert session.verify is True
            assert session.auth.project_id == "project"
            assert connection.config.get_interface() == "public"
            close = Mock(wraps=session.session.close)
            monkeypatch.setattr(session.session, "close", close)
        # Connection construction and closing require no authentication/network.
    close.assert_called_once()
    assert session.request_timeout == (5, 20)


def test_application_credential_connection():
    settings = Settings(
        api_token="token",
        project_id="project",
        auth_url="https://keystone/v3",
        auth_type="application_credential",
        application_credential_id="app",
        application_credential_secret="secret",
    )
    with open_connection(settings) as connection:
        assert connection.session.auth.auth_methods[0].application_credential_id == "app"


def test_sdk_create_serialization():
    resource = SDKServer.new(name="web", image_id="i", flavor_id="f", networks=[{"uuid": "n"}])
    assert resource._prepare_request(requires_id=False).body == {
        "server": {"name": "web", "imageRef": "i", "flavorRef": "f", "networks": [{"uuid": "n"}]}
    }


def test_ready_preserves_shorter_timeout_and_restores_on_error(conn):
    session = ControllerSession(connect_timeout=0.5, read_timeout=1, ready_timeout=3)
    session.get_project_id = Mock(return_value="project")
    conn.session = session

    def fail(**kwargs):
        assert session.request_timeout == (0.5, 1)
        raise ke.ConnectTimeout("secret")

    conn.compute.servers.side_effect = fail
    with pytest.raises(UpstreamTimeout):
        OpenStackHealthAdapter(conn, "project", "provider").check_ready(CTX)
    assert session.request_timeout == (0.5, 1)
    session.session.close()


def test_connection_close_failure_still_closes_http_pool(monkeypatch):
    settings = Settings(
        api_token="t",
        project_id="p",
        auth_url="https://keystone/v3",
        username="u",
        password="p",
        user_domain_name="Default",
    )
    with pytest.raises(RuntimeError, match="close failed"):
        with open_connection(settings) as connection:
            close = Mock(wraps=connection.session.session.close)
            monkeypatch.setattr(connection.session.session, "close", close)
            monkeypatch.setattr(connection, "close", Mock(side_effect=RuntimeError("close failed")))
    close.assert_called_once()


def test_identity_auth_failure_not_caller_forbidden(conn):
    conn.session.get_project_id.side_effect = ke.Forbidden("secret")
    with pytest.raises(UpstreamFailure):
        OpenStackComputeAdapter(conn, "project", "provider").get_server(CTX, "s")


def test_mutation_redirect_not_reported_as_accepted():
    session = ControllerSession(connect_timeout=5, read_timeout=20, ready_timeout=2)
    response = Response()
    response.status_code = 307
    with patch("keystoneauth1.session.Session.request", return_value=response):
        with pytest.raises(oe.HttpException):
            session.request("https://cloud", "POST")
    session.session.close()


def real_compute_proxy():
    from openstack.compute.v2._proxy import Proxy

    session = ControllerSession(connect_timeout=1, read_timeout=1, ready_timeout=1)
    session.get_project_id = Mock(return_value="project")
    proxy = Proxy(session=session, endpoint_override="https://nova/v2.1")
    proxy.get_endpoint_data = Mock(return_value=None)
    return SimpleNamespace(compute=proxy, session=session)


def json_response(body, status=200):
    import json

    response = Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.headers["Content-Type"] = "application/json"
    return response


def test_actual_proxy_create_http_boundary():
    connection = real_compute_proxy()
    connection.compute.post = Mock(
        return_value=json_response({"server": {"id": "s", "adminPass": "secret"}}, 202)
    )
    try:
        result = OpenStackComputeAdapter(connection, "project", "provider").create_server(CTX, SPEC)
        assert result.resource_id == "s"
        call = connection.compute.post.call_args
        assert call.args == ("/servers",)
        assert call.kwargs["json"] == {
            "server": {
                "name": "web",
                "imageRef": "image",
                "flavorRef": "flavor",
                "networks": [{"uuid": "network"}],
            }
        }
    finally:
        connection.session.session.close()


def test_actual_proxy_list_http_boundary():
    connection = real_compute_proxy()
    connection.compute.get = Mock(
        return_value=json_response(
            {
                "servers": [
                    {"id": str(i), "tenant_id": "project", "name": "web", "status": "ACTIVE"}
                    for i in range(3)
                ]
            }
        )
    )
    try:
        result = OpenStackComputeAdapter(connection, "project", "provider").list_servers(
            CTX, PageRequest(2, "old")
        )
        assert len(result.items) == 2
        assert result.next_marker == "1"
        connection.compute.get.assert_called_once()
        call = connection.compute.get.call_args
        assert call.args == ("/servers/detail",)
        assert call.kwargs["params"] == {"limit": 3, "marker": "old", "project_id": "project"}
    finally:
        connection.session.session.close()


@pytest.mark.parametrize(
    "action,payload",
    [
        ("start", {"os-start": None}),
        ("stop", {"os-stop": None}),
        ("reboot", {"reboot": {"type": "SOFT"}}),
    ],
)
def test_actual_proxy_action_http_boundary(action, payload):
    connection = real_compute_proxy()
    connection.compute.get = Mock(
        return_value=json_response({"server": {"id": "s", "tenant_id": "project"}})
    )
    connection.compute.post = Mock(return_value=json_response({}, 202))
    try:
        result = OpenStackComputeAdapter(connection, "project", "provider").act_server(
            CTX, "s", action
        )
        assert result.action == action
        call = connection.compute.post.call_args
        assert call.args == ("servers/s/action",)
        assert call.kwargs["json"] == payload
    finally:
        connection.session.session.close()


def test_upstream_logging_correlates_ids_without_private_response(caplog):
    import logging

    session = ControllerSession(connect_timeout=1, read_timeout=1, ready_timeout=1)
    session.request_id = "9cd16c11-e380-4c48-8b97-d9dbdfbdf200"
    response = json_response({"adminPass": "private-password"})
    response.headers["X-Openstack-Request-Id"] = "req-384ef2c6-67b7-4f06-a95f-e1f645641bf2"
    with caplog.at_level(logging.INFO, logger="control_plane.upstream"):
        with patch("keystoneauth1.session.Session.request", return_value=response):
            session.request("https://private-host/secret-path", "GET")
    assert session.request_id in caplog.text
    assert response.headers["X-Openstack-Request-Id"] in caplog.text
    assert "private-password" not in caplog.text
    assert "private-host" not in caplog.text
    session.session.close()
