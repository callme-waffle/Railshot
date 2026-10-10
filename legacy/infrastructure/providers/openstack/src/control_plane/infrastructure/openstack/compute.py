from typing import Any

from openstack.exceptions import NotFoundException

from control_plane.domain.errors import InvalidInput, NotFound, UpstreamFailure
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
from control_plane.infrastructure.openstack import mapping
from control_plane.infrastructure.openstack.base import ScopedAdapter
from control_plane.infrastructure.openstack.errors import upstream


class OpenStackComputeAdapter(ScopedAdapter):
    def _get_owned(self, server_id: str) -> Any:
        with upstream():
            result = self.connection.compute.get_server(server_id)
        if mapping.text_field(result, "id") != server_id:
            raise UpstreamFailure()
        if mapping.text_field(result, "project_id") != self.project_id:
            raise NotFound()
        return result

    def list_servers(self, ctx: RequestContext, page: PageRequest) -> Page[Server]:
        self.check_scope(ctx)

        def convert(resource: Any) -> Server:
            result = mapping.server(resource)
            if result.project_id != self.project_id:
                raise UpstreamFailure()
            return result

        with upstream():
            return mapping.page_result(
                self.connection.compute.servers(
                    details=True, project_id=self.project_id, **mapping.page_query(page)
                ),
                page,
                convert,
            )

    def get_server(self, ctx: RequestContext, server_id: str) -> Server:
        self.check_scope(ctx)
        return mapping.server(self._get_owned(server_id))

    def create_server(self, ctx: RequestContext, spec: CreateServerSpec) -> Accepted:
        self.check_scope(ctx)
        with upstream(mutation=True):
            result = self.connection.compute.create_server(
                name=spec.name,
                image_id=spec.image_id,
                flavor_id=spec.flavor_id,
                networks=[{"uuid": value} for value in spec.network_ids],
            )
            try:
                resource_id = mapping.text_field(result, "id")
                # Nova create response can omit tenant_id; scoped token enforces ownership.
                project_id = getattr(result, "project_id", None)
                if project_id is not None and project_id != self.project_id:
                    raise UpstreamFailure()
            except UpstreamFailure as exc:
                raise UpstreamFailure(outcome_unknown=True) from exc
        return Accepted(resource_id, "create")

    def delete_server(self, ctx: RequestContext, server_id: str) -> DeleteResult:
        self.check_scope(ctx)
        # Distinguish actual absence from an existing cross-project resource.
        with upstream():
            try:
                result = self.connection.compute.get_server(server_id)
            except NotFoundException:
                return DeleteResult(server_id, True)
        if mapping.text_field(result, "id") != server_id:
            raise UpstreamFailure()
        if mapping.text_field(result, "project_id") != self.project_id:
            raise NotFound()
        with upstream(mutation=True):
            self.connection.compute.delete_server(server_id, ignore_missing=True)
        return DeleteResult(server_id, False)

    def act_server(self, ctx: RequestContext, server_id: str, action: ServerAction) -> Accepted:
        self.check_scope(ctx)
        if action not in ("start", "stop", "reboot"):
            raise InvalidInput()
        self._get_owned(server_id)
        with upstream(mutation=True):
            if action == "start":
                self.connection.compute.start_server(server_id)
            elif action == "stop":
                self.connection.compute.stop_server(server_id)
            else:
                self.connection.compute.reboot_server(server_id, reboot_type="SOFT")
        return Accepted(server_id, action)
