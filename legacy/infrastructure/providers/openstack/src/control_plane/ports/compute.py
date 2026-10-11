from __future__ import annotations

from typing import Protocol

from control_plane.domain.models import (
    Accepted,
    CreateServerSpec,
    DeleteResult,
    Page,
    PageRequest,
    RequestContext,
    Server,
    ServerAction,
)


class ComputeProvider(Protocol):
    def list_servers(self, ctx: RequestContext, page: PageRequest) -> Page[Server]: ...
    def get_server(self, ctx: RequestContext, server_id: str) -> Server: ...
    def create_server(self, ctx: RequestContext, spec: CreateServerSpec) -> Accepted: ...
    def delete_server(self, ctx: RequestContext, server_id: str) -> DeleteResult: ...
    def act_server(self, ctx: RequestContext, server_id: str, action: ServerAction) -> Accepted: ...
