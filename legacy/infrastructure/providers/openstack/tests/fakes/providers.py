from dataclasses import replace
from threading import RLock
from uuid import uuid4

from control_plane.bootstrap import ProviderBundle
from control_plane.domain.errors import InvalidInput, NotFound
from control_plane.domain.models import (
    Accepted,
    CreateServerSpec,
    DeleteResult,
    Flavor,
    Image,
    Network,
    Page,
    PageRequest,
    RequestContext,
    Server,
    ServerAction,
)


class FakeCloud:
    """Deterministic, in-memory test double. This never contacts OpenStack."""

    def __init__(self) -> None:
        self.servers: dict[str, Server] = {}
        self.calls: list[str] = []
        self.lock = RLock()
        self.ready = True

    def bundle(self) -> ProviderBundle:
        return ProviderBundle(compute=self, catalog=self, health=self)

    def list_servers(self, ctx: RequestContext, page: PageRequest) -> Page[Server]:
        with self.lock:
            self.calls.append("list_servers")
            rows = sorted(self.servers.values(), key=lambda row: row.id)
            if page.marker is not None:
                rows = [row for row in rows if row.id > page.marker]
            return Page(
                tuple(rows[: page.limit]),
                rows[page.limit - 1].id if len(rows) > page.limit else None,
            )

    def get_server(self, ctx: RequestContext, server_id: str) -> Server:
        with self.lock:
            self.calls.append("get_server")
            if server_id not in self.servers:
                raise NotFound()
            return self.servers[server_id]

    def create_server(self, ctx: RequestContext, spec: CreateServerSpec) -> Accepted:
        with self.lock:
            self.calls.append("create_server")
            resource_id = str(uuid4())
            self.servers[resource_id] = Server(resource_id, spec.name, ctx.project_id, "ACTIVE", ())
            return Accepted(resource_id, "create")

    def delete_server(self, ctx: RequestContext, server_id: str) -> DeleteResult:
        with self.lock:
            self.calls.append("delete_server")
            return DeleteResult(server_id, self.servers.pop(server_id, None) is None)

    def act_server(self, ctx: RequestContext, server_id: str, action: ServerAction) -> Accepted:
        with self.lock:
            self.calls.append("act_server")
            current = self.get_server(ctx, server_id)
            self.servers[server_id] = replace(
                current, status="SHUTOFF" if action == "stop" else "ACTIVE"
            )
            return Accepted(server_id, action)

    def list_images(self, ctx: RequestContext, page: PageRequest) -> Page[Image]:
        self.calls.append("list_images")
        return Page((Image("image-test", "test image", "active"),), None)

    def list_flavors(self, ctx: RequestContext, page: PageRequest) -> Page[Flavor]:
        self.calls.append("list_flavors")
        return Page((Flavor("flavor-test", "test small", 2, 2048, 10),), None)

    def list_networks(self, ctx: RequestContext, page: PageRequest) -> Page[Network]:
        self.calls.append("list_networks")
        return Page((Network("network-test", "test network", "ACTIVE", False),), None)

    def validate_create_references(self, ctx: RequestContext, spec: CreateServerSpec) -> None:
        self.calls.append("validate_create_references")
        if (
            spec.image_id != "image-test"
            or spec.flavor_id != "flavor-test"
            or spec.network_ids != ("network-test",)
        ):
            raise InvalidInput()

    def check_ready(self, ctx: RequestContext) -> bool:
        self.calls.append("check_ready")
        return self.ready
