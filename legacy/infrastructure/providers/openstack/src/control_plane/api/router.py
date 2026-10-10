"""Synchronous HTTP endpoints; business policy remains in application services."""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from control_plane.api.schemas import (
    AcceptedResponse,
    CreateServerRequest,
    FlavorResponse,
    HealthResponse,
    ImageResponse,
    NetworkResponse,
    PageResponse,
    ServerActionRequest,
    ServerResponse,
)
from control_plane.application.catalog import CatalogService
from control_plane.application.compute import ComputeService
from control_plane.application.health import HealthService
from control_plane.bootstrap import (
    get_catalog_service,
    get_compute_service,
    get_context,
    get_health_service,
)
from control_plane.domain.errors import InvalidInput, UpstreamUnavailable
from control_plane.domain.models import Accepted, CreateServerSpec, PageRequest, RequestContext

router = APIRouter()
Context = Annotated[RequestContext, Depends(get_context)]
Compute = Annotated[ComputeService, Depends(get_compute_service)]
Catalog = Annotated[CatalogService, Depends(get_catalog_service)]
Health = Annotated[HealthService, Depends(get_health_service)]
ServerId = Annotated[str, Path(min_length=1, pattern=r"\S")]


def get_page(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    marker: Annotated[str | None, Query(min_length=1, pattern=r"\S")] = None,
) -> PageRequest:
    if any(key not in {"limit", "marker"} for key in request.query_params):
        raise InvalidInput()
    if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise InvalidInput()
    return PageRequest(limit=limit, marker=marker)


Pagination = Annotated[PageRequest, Depends(get_page)]


def _accepted(result: Accepted, ctx: RequestContext, response: Response) -> AcceptedResponse:
    response.headers["Location"] = f"/api/v1/servers/{quote(result.resource_id, safe='')}"
    return AcceptedResponse(
        resource_id=result.resource_id,
        action=result.action,
        request_id=ctx.request_id,
    )


@router.get("/health/live", response_model=HealthResponse)
def live() -> HealthResponse:
    return HealthResponse()


@router.get("/health/ready", response_model=HealthResponse)
def ready(ctx: Context, service: Health) -> HealthResponse:
    if not service.check_ready(ctx):
        raise UpstreamUnavailable()
    return HealthResponse()


@router.get("/api/v1/servers", response_model=PageResponse[ServerResponse])
def list_servers(ctx: Context, service: Compute, page: Pagination) -> PageResponse[ServerResponse]:
    result = service.list_servers(ctx, page)
    return PageResponse(
        items=[ServerResponse.model_validate(item) for item in result.items],
        next_marker=result.next_marker,
    )


@router.get("/api/v1/servers/{server_id}", response_model=ServerResponse)
def get_server(server_id: ServerId, ctx: Context, service: Compute) -> ServerResponse:
    return ServerResponse.model_validate(service.get_server(ctx, server_id))


@router.post("/api/v1/servers", status_code=202, response_model=AcceptedResponse)
def create_server(
    body: CreateServerRequest,
    ctx: Context,
    service: Compute,
    response: Response,
) -> AcceptedResponse:
    spec = CreateServerSpec(
        name=body.name,
        image_id=body.image_id,
        flavor_id=body.flavor_id,
        network_ids=tuple(body.network_ids),
    )
    return _accepted(service.create_server(ctx, spec), ctx, response)


@router.delete(
    "/api/v1/servers/{server_id}",
    status_code=202,
    response_model=AcceptedResponse,
    responses={204: {"description": "Server already absent"}},
)
def delete_server(
    server_id: ServerId,
    ctx: Context,
    service: Compute,
    response: Response,
) -> AcceptedResponse | Response:
    result = service.delete_server(ctx, server_id)
    if result.already_absent:
        return Response(status_code=204)
    return _accepted(Accepted(resource_id=result.resource_id, action="delete"), ctx, response)


@router.post(
    "/api/v1/servers/{server_id}/actions",
    status_code=202,
    response_model=AcceptedResponse,
)
def act_server(
    server_id: ServerId,
    body: ServerActionRequest,
    ctx: Context,
    service: Compute,
    response: Response,
) -> AcceptedResponse:
    return _accepted(service.act_server(ctx, server_id, body.action), ctx, response)


@router.get("/api/v1/images", response_model=PageResponse[ImageResponse])
def list_images(ctx: Context, service: Catalog, page: Pagination) -> PageResponse[ImageResponse]:
    result = service.list_images(ctx, page)
    return PageResponse(
        items=[ImageResponse.model_validate(item) for item in result.items],
        next_marker=result.next_marker,
    )


@router.get("/api/v1/flavors", response_model=PageResponse[FlavorResponse])
def list_flavors(ctx: Context, service: Catalog, page: Pagination) -> PageResponse[FlavorResponse]:
    result = service.list_flavors(ctx, page)
    return PageResponse(
        items=[FlavorResponse.model_validate(item) for item in result.items],
        next_marker=result.next_marker,
    )


@router.get("/api/v1/networks", response_model=PageResponse[NetworkResponse])
def list_networks(
    ctx: Context, service: Catalog, page: Pagination
) -> PageResponse[NetworkResponse]:
    result = service.list_networks(ctx, page)
    return PageResponse(
        items=[NetworkResponse.model_validate(item) for item in result.items],
        next_marker=result.next_marker,
    )
