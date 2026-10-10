import ast
import inspect
from pathlib import Path
from typing import get_type_hints

import pytest

from control_plane.application.catalog import CatalogService
from control_plane.application.compute import ComputeService
from control_plane.application.health import HealthService
from control_plane.ports.catalog import CatalogProvider
from control_plane.ports.compute import ComputeProvider
from control_plane.ports.health import HealthProvider

ROOT = Path(__file__).resolve().parents[2] / "src" / "control_plane"


@pytest.mark.parametrize(
    "layer,forbidden",
    [
        (
            "domain",
            (
                "fastapi",
                "pydantic",
                "openstack",
                "control_plane.application",
                "control_plane.ports",
                "control_plane.infrastructure",
                "control_plane.api",
            ),
        ),
        (
            "ports",
            (
                "fastapi",
                "pydantic",
                "openstack",
                "control_plane.application",
                "control_plane.infrastructure",
                "control_plane.api",
            ),
        ),
        (
            "application",
            (
                "fastapi",
                "pydantic",
                "openstack",
                "control_plane.infrastructure",
                "control_plane.api",
                "control_plane.config",
                "os",
            ),
        ),
        ("api", ("openstack", "control_plane.infrastructure")),
        ("infrastructure", ("fastapi", "control_plane.application", "control_plane.api")),
    ],
)
def test_layer_import_boundaries(layer, forbidden):
    for path in (ROOT / layer).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                assert not any(
                    module == bad or module.startswith(bad + ".") for bad in forbidden
                ), (path, module)


@pytest.mark.parametrize(
    "service,provider,methods",
    [
        (
            ComputeService,
            ComputeProvider,
            ["list_servers", "get_server", "create_server", "delete_server", "act_server"],
        ),
        (CatalogService, CatalogProvider, ["list_images", "list_flavors", "list_networks"]),
        (HealthService, HealthProvider, ["check_ready"]),
    ],
)
def test_service_provider_signatures(service, provider, methods):
    for method in methods:
        implementation, contract = getattr(service, method), getattr(provider, method)
        assert list(inspect.signature(implementation).parameters) == list(
            inspect.signature(contract).parameters
        )
        assert get_type_hints(implementation) == get_type_hints(contract)
