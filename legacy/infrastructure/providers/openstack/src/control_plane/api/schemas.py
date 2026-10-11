"""HTTP representations, kept separate from provider and domain objects."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.domain.models import ResourceAction, ServerAction

Identifier = Annotated[str, Field(min_length=1, pattern=r"\S")]


class CreateServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Business length is checked after trimming by ComputeService.
    name: Annotated[str, Field(min_length=1, pattern=r"\S")]
    image_id: Identifier
    flavor_id: Identifier
    network_ids: Annotated[list[Identifier], Field(min_length=1)]


class ServerActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: ServerAction


class ResourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AddressResponse(ResourceResponse):
    network: str
    address: str
    version: Literal[4, 6]


class ServerResponse(ResourceResponse):
    id: str
    name: str
    project_id: str
    status: str
    addresses: list[AddressResponse]


class ImageResponse(ResourceResponse):
    id: str
    name: str
    status: str


class FlavorResponse(ResourceResponse):
    id: str
    name: str
    vcpus: int
    ram_mb: int
    disk_gb: int


class NetworkResponse(ResourceResponse):
    id: str
    name: str
    status: str
    shared: bool


class PageResponse[T](BaseModel):
    items: list[T]
    next_marker: str | None


class AcceptedResponse(BaseModel):
    resource_id: str
    action: ResourceAction
    status: Literal["accepted"] = "accepted"
    request_id: str


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    retryable: bool
    outcome_unknown: bool


class ErrorResponse(BaseModel):
    error: ErrorDetail
