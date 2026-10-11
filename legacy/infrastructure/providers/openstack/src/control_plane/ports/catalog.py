from __future__ import annotations

from typing import Protocol

from control_plane.domain.models import (
    CreateServerSpec,
    Flavor,
    Image,
    Network,
    Page,
    PageRequest,
    RequestContext,
)


class CatalogProvider(Protocol):
    def list_images(self, ctx: RequestContext, page: PageRequest) -> Page[Image]: ...
    def list_flavors(self, ctx: RequestContext, page: PageRequest) -> Page[Flavor]: ...
    def list_networks(self, ctx: RequestContext, page: PageRequest) -> Page[Network]: ...
    def validate_create_references(self, ctx: RequestContext, spec: CreateServerSpec) -> None: ...
