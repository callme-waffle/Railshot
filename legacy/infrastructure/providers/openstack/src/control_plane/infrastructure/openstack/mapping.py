"""Validate and select public fields from SDK resources."""

from collections.abc import Callable, Iterable, Mapping
from ipaddress import ip_address
from itertools import islice
from typing import Any, TypeVar

from control_plane.domain.errors import UpstreamFailure
from control_plane.domain.models import Address, Flavor, Image, Network, Page, PageRequest, Server

T = TypeVar("T")


def text_field(resource: Any, key: str, *, default: str | None = None) -> str:
    value = getattr(resource, key, None)
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or (default is None and not value.strip()):
        raise UpstreamFailure()
    return value


def number(resource: Any, key: str, *, minimum: int = 0) -> int:
    value = getattr(resource, key, None)
    if type(value) is not int or value < minimum:
        raise UpstreamFailure()
    return value


def server(resource: Any) -> Server:
    addresses = []
    raw = getattr(resource, "addresses", None) or {}
    if not isinstance(raw, Mapping):
        raise UpstreamFailure()
    for network, entries in raw.items():
        if not isinstance(network, str) or not isinstance(entries, list):
            raise UpstreamFailure()
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise UpstreamFailure()
            address, version = entry.get("addr"), entry.get("version")
            if not isinstance(address, str) or type(version) is not int or version not in (4, 6):
                raise UpstreamFailure()
            try:
                if ip_address(address).version != version:
                    raise UpstreamFailure()
            except ValueError as exc:
                raise UpstreamFailure() from exc
            addresses.append(Address(network, address, 4 if version == 4 else 6))
    return Server(
        text_field(resource, "id"),
        text_field(resource, "name", default=""),
        text_field(resource, "project_id"),
        text_field(resource, "status", default="UNKNOWN"),
        tuple(addresses),
    )


def image(resource: Any) -> Image:
    return Image(
        text_field(resource, "id"),
        text_field(resource, "name", default=""),
        text_field(resource, "status", default="UNKNOWN"),
    )


def flavor(resource: Any) -> Flavor:
    return Flavor(
        text_field(resource, "id"),
        text_field(resource, "name", default=""),
        number(resource, "vcpus", minimum=1),
        number(resource, "ram", minimum=1),
        number(resource, "disk"),
    )


def network(resource: Any) -> Network:
    shared = getattr(resource, "is_shared", None)
    if type(shared) is not bool:
        raise UpstreamFailure()
    return Network(
        text_field(resource, "id"),
        text_field(resource, "name", default=""),
        text_field(resource, "status", default="UNKNOWN"),
        shared,
    )


def page_result[T](
    resources: Iterable[Any], page: PageRequest, convert: Callable[[Any], T]
) -> Page[T]:
    # Consume only the requested page and one look-ahead; SDK handles native paging.
    raw = list(islice(resources, page.limit + 1))
    converted = tuple(convert(item) for item in raw)
    marker = text_field(raw[page.limit - 1], "id") if len(raw) > page.limit else None
    return Page(converted[: page.limit], marker)


def page_query(page: PageRequest) -> dict[str, Any]:
    query: dict[str, Any] = {"limit": page.limit + 1, "max_items": page.limit + 1}
    if page.marker is not None:
        query["marker"] = page.marker
    return query
