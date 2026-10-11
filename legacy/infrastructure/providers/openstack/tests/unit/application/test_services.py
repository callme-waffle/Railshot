from dataclasses import replace
from unittest.mock import Mock, call

import pytest

from control_plane.application.catalog import CatalogService
from control_plane.application.compute import ComputeService
from control_plane.application.health import HealthService
from control_plane.domain.errors import (
    Conflict,
    Forbidden,
    InternalFailure,
    InvalidInput,
    NotFound,
    RateLimited,
    UpstreamFailure,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from control_plane.domain.models import (
    Accepted,
    CreateServerSpec,
    DeleteResult,
    Page,
    PageRequest,
    RequestContext,
)
from control_plane.ports.catalog import CatalogProvider
from control_plane.ports.compute import ComputeProvider
from control_plane.ports.health import HealthProvider

CTX = RequestContext("request-1", "operator", "project-1", "provider-1")
SPEC = CreateServerSpec("web", "image-1", "small", ("network-1",))


@pytest.fixture
def providers():
    return Mock(spec=ComputeProvider), Mock(spec=CatalogProvider), Mock(spec=HealthProvider)


@pytest.fixture
def services(providers):
    compute, catalog, health = providers
    return (
        ComputeService(compute, catalog, CTX.project_id, CTX.provider_id),
        CatalogService(catalog, CTX.project_id, CTX.provider_id),
        HealthService(health, CTX.project_id, CTX.provider_id),
    )


OPERATIONS = [
    (0, "list_servers", (PageRequest(),)),
    (0, "get_server", ("server-1",)),
    (0, "create_server", (SPEC,)),
    (0, "delete_server", ("server-1",)),
    (0, "act_server", ("server-1", "start")),
    (1, "list_images", (PageRequest(),)),
    (1, "list_flavors", (PageRequest(),)),
    (1, "list_networks", (PageRequest(),)),
    (2, "check_ready", ()),
]


@pytest.mark.parametrize("field", ["project_id", "provider_id"])
@pytest.mark.parametrize("index,method,args", OPERATIONS)
def test_every_operation_rejects_wrong_scope_before_provider_calls(
    services, providers, field, index, method, args
):
    ctx = replace(CTX, **{field: "other"})
    with pytest.raises(Forbidden):
        getattr(services[index], method)(ctx, *args)
    assert all(not provider.mock_calls for provider in providers)


def test_create_normalizes_name_without_mutating_input_and_checks_refs_first(services, providers):
    compute, catalog, _ = providers
    calls = Mock()
    calls.attach_mock(catalog.validate_create_references, "validate")
    calls.attach_mock(compute.create_server, "create")
    expected = Accepted("server-1", "create")
    compute.create_server.return_value = expected
    original = replace(SPEC, name="  web\n")

    assert services[0].create_server(CTX, original) is expected
    assert original.name == "  web\n"
    assert calls.mock_calls == [call.validate(CTX, SPEC), call.create(CTX, SPEC)]


@pytest.mark.parametrize("error_type", [InvalidInput, Forbidden, UpstreamTimeout])
def test_failed_reference_validation_prevents_create(services, providers, error_type):
    compute, catalog, _ = providers
    error = error_type()
    catalog.validate_create_references.side_effect = error
    with pytest.raises(error_type) as caught:
        services[0].create_server(CTX, SPEC)
    assert caught.value is error
    compute.create_server.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"name": " \n "},
        {"name": "a" * 256},
        {"name": 123},
        {"image_id": " "},
        {"flavor_id": ""},
        {"flavor_id": 1},
        {"network_ids": ()},
        {"network_ids": ("net", "net")},
        {"network_ids": ("",)},
        {"network_ids": (True,)},
        {"network_ids": "network"},
        {"network_ids": ["network"]},
    ],
)
def test_invalid_create_input_never_reaches_provider(services, providers, changes):
    with pytest.raises(InvalidInput):
        services[0].create_server(CTX, replace(SPEC, **changes))
    assert all(not provider.mock_calls for provider in providers)


@pytest.mark.parametrize("name", ["x", "x" * 255])
def test_valid_name_boundaries(services, providers, name):
    services[0].create_server(CTX, replace(SPEC, name=f" {name} "))
    assert providers[0].create_server.call_args.args[1].name == name


LIST_OPERATIONS = [
    (0, "list_servers"),
    (1, "list_images"),
    (1, "list_flavors"),
    (1, "list_networks"),
]


@pytest.mark.parametrize("index,method", LIST_OPERATIONS)
@pytest.mark.parametrize("limit", [0, 101, -1, True, False, 1.0, "20", None])
def test_page_limit_rejects_invalid_values(services, providers, index, method, limit):
    with pytest.raises(InvalidInput):
        getattr(services[index], method)(CTX, PageRequest(limit=limit))
    assert all(not provider.mock_calls for provider in providers)


@pytest.mark.parametrize("index,method", LIST_OPERATIONS)
@pytest.mark.parametrize("marker", ["", " \n ", 1, True])
def test_page_marker_rejects_invalid_values(services, providers, index, method, marker):
    with pytest.raises(InvalidInput):
        getattr(services[index], method)(CTX, PageRequest(marker=marker))
    assert all(not provider.mock_calls for provider in providers)


@pytest.mark.parametrize("index,method", LIST_OPERATIONS)
@pytest.mark.parametrize("limit", [1, 100])
def test_list_preserves_pagination_and_result(services, providers, index, method, limit):
    page = PageRequest(limit=limit, marker="opaque-id")
    expected = Page((), "next-id")
    provider_method = getattr(providers[index], method)
    provider_method.return_value = expected
    assert getattr(services[index], method)(CTX, page) is expected
    provider_method.assert_called_once_with(CTX, page)


@pytest.mark.parametrize(
    "method,args", [("get_server", ()), ("delete_server", ()), ("act_server", ("start",))]
)
@pytest.mark.parametrize("server_id", ["", " ", None, 1])
def test_invalid_id_rejected(services, providers, method, args, server_id):
    with pytest.raises(InvalidInput):
        getattr(services[0], method)(CTX, server_id, *args)
    assert all(not provider.mock_calls for provider in providers)


@pytest.mark.parametrize("action", ["hard_reboot", "START", "", None, True, ["start"]])
def test_unsupported_action_rejected_without_call(services, providers, action):
    with pytest.raises(InvalidInput):
        services[0].act_server(CTX, "server-1", action)
    providers[0].act_server.assert_not_called()


@pytest.mark.parametrize("action", ["start", "stop", "reboot"])
def test_supported_actions_always_reach_provider(services, providers, action):
    expected = Accepted("server-1", action)
    providers[0].act_server.return_value = expected
    assert services[0].act_server(CTX, "server-1", action) is expected
    providers[0].act_server.assert_called_once_with(CTX, "server-1", action)
    providers[0].get_server.assert_not_called()


@pytest.mark.parametrize("absent", [True, False])
def test_delete_preserves_provider_result(services, providers, absent):
    expected = DeleteResult("server-1", absent)
    providers[0].delete_server.return_value = expected
    assert services[0].delete_server(CTX, "server-1") is expected
    providers[0].delete_server.assert_called_once_with(CTX, "server-1")


@pytest.mark.parametrize("error_type", [NotFound, Forbidden, Conflict, UpstreamUnavailable])
def test_delete_does_not_suppress_errors(services, providers, error_type):
    error = error_type()
    providers[0].delete_server.side_effect = error
    with pytest.raises(error_type) as caught:
        services[0].delete_server(CTX, "server-1")
    assert caught.value is error


def test_unknown_create_outcome_is_preserved_without_retry(services, providers):
    error = UpstreamTimeout(outcome_unknown=True, retryable=False)
    providers[0].create_server.side_effect = error
    with pytest.raises(UpstreamTimeout) as caught:
        services[0].create_server(CTX, SPEC)
    assert caught.value is error
    assert caught.value.outcome_unknown
    assert not caught.value.retryable
    providers[0].create_server.assert_called_once()


@pytest.mark.parametrize("ready", [True, False])
def test_health_preserves_provider_state(services, providers, ready):
    providers[2].check_ready.return_value = ready
    assert services[2].check_ready(CTX) is ready


@pytest.mark.parametrize(
    "error_type", [UpstreamFailure, UpstreamUnavailable, UpstreamTimeout, RateLimited]
)
def test_health_expected_upstream_errors_mean_not_ready(services, providers, error_type):
    providers[2].check_ready.side_effect = error_type()
    assert services[2].check_ready(CTX) is False


@pytest.mark.parametrize("error_type", [Forbidden, InvalidInput, InternalFailure, RuntimeError])
def test_health_does_not_hide_scope_or_programming_errors(services, providers, error_type):
    error = error_type()
    providers[2].check_ready.side_effect = error
    with pytest.raises(error_type) as caught:
        services[2].check_ready(CTX)
    assert caught.value is error
