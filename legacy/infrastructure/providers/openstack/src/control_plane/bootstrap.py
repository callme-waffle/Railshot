from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from control_plane.application.catalog import CatalogService
from control_plane.application.compute import ComputeService
from control_plane.application.health import HealthService
from control_plane.config import Settings
from control_plane.domain.errors import Unauthenticated
from control_plane.domain.models import RequestContext
from control_plane.ports.catalog import CatalogProvider
from control_plane.ports.compute import ComputeProvider
from control_plane.ports.health import HealthProvider


@dataclass(frozen=True)
class ProviderBundle:
    compute: ComputeProvider
    catalog: CatalogProvider
    health: HealthProvider


def get_context(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(HTTPBearer(auto_error=False))
    ],
) -> RequestContext:
    context = getattr(request.state, "context", None)
    if not isinstance(context, RequestContext):
        raise Unauthenticated()
    return context


def get_providers(
    request: Request, ctx: Annotated[RequestContext, Depends(get_context)]
) -> Iterator[ProviderBundle]:
    injected = request.app.state.providers
    if injected is not None:
        yield cast(ProviderBundle, injected)
        return
    from control_plane.infrastructure.openstack.catalog import OpenStackCatalogAdapter
    from control_plane.infrastructure.openstack.compute import OpenStackComputeAdapter
    from control_plane.infrastructure.openstack.connection import ControllerSession, open_connection
    from control_plane.infrastructure.openstack.health import OpenStackHealthAdapter

    settings: Settings = request.app.state.settings
    if request.url.path == "/health/ready":
        settings = settings.model_copy(
            update={
                "connect_timeout": min(settings.connect_timeout, settings.ready_timeout),
                "read_timeout": min(settings.read_timeout, settings.ready_timeout),
            }
        )
    with open_connection(settings) as connection:
        if isinstance(connection.session, ControllerSession):
            connection.session.request_id = ctx.request_id
        yield ProviderBundle(
            compute=OpenStackComputeAdapter(connection, ctx.project_id, ctx.provider_id),
            catalog=OpenStackCatalogAdapter(connection, ctx.project_id, ctx.provider_id),
            health=OpenStackHealthAdapter(connection, ctx.project_id, ctx.provider_id),
        )


def get_compute_service(
    request: Request, providers: Annotated[ProviderBundle, Depends(get_providers)]
) -> ComputeService:
    settings: Settings = request.app.state.settings
    return ComputeService(
        providers.compute, providers.catalog, settings.project_id, settings.provider_id
    )


def get_catalog_service(
    request: Request, providers: Annotated[ProviderBundle, Depends(get_providers)]
) -> CatalogService:
    settings: Settings = request.app.state.settings
    return CatalogService(providers.catalog, settings.project_id, settings.provider_id)


def get_health_service(
    request: Request, providers: Annotated[ProviderBundle, Depends(get_providers)]
) -> HealthService:
    settings: Settings = request.app.state.settings
    return HealthService(providers.health, settings.project_id, settings.provider_id)
