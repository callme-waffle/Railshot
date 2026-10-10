from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeVar

T = TypeVar("T")
ServerAction = Literal["start", "stop", "reboot"]
ResourceAction = Literal["create", "delete", "start", "stop", "reboot"]


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    principal_id: str
    project_id: str
    provider_id: str


@dataclass(frozen=True)
class PageRequest:
    limit: int = 20
    marker: str | None = None


@dataclass(frozen=True)
class Page[T]:
    items: tuple[T, ...]
    next_marker: str | None


@dataclass(frozen=True)
class Address:
    network: str
    address: str
    version: Literal[4, 6]


@dataclass(frozen=True)
class Server:
    id: str
    name: str
    project_id: str
    status: str
    addresses: tuple[Address, ...]


@dataclass(frozen=True)
class Image:
    id: str
    name: str
    status: str


@dataclass(frozen=True)
class Flavor:
    id: str
    name: str
    vcpus: int
    ram_mb: int
    disk_gb: int


@dataclass(frozen=True)
class Network:
    id: str
    name: str
    status: str
    shared: bool


@dataclass(frozen=True)
class CreateServerSpec:
    name: str
    image_id: str
    flavor_id: str
    network_ids: tuple[str, ...]


@dataclass(frozen=True)
class Accepted:
    resource_id: str
    action: ResourceAction


@dataclass(frozen=True)
class DeleteResult:
    resource_id: str
    already_absent: bool
