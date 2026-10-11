from control_plane.application.validation import (
    normalize_create_spec,
    validate_context,
    validate_id,
    validate_page,
)
from control_plane.domain.errors import InvalidInput
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
from control_plane.ports.catalog import CatalogProvider
from control_plane.ports.compute import ComputeProvider


class ComputeService:
    def __init__(
        self,
        compute: ComputeProvider,
        catalog: CatalogProvider,
        project_id: str,
        provider_id: str,
    ) -> None:
        self._compute = compute
        self._catalog = catalog
        self._project_id = project_id
        self._provider_id = provider_id

    def list_servers(self, ctx: RequestContext, page: PageRequest) -> Page[Server]:
        validate_context(ctx, self._project_id, self._provider_id)
        validate_page(page)
        return self._compute.list_servers(ctx, page)

    def get_server(self, ctx: RequestContext, server_id: str) -> Server:
        validate_context(ctx, self._project_id, self._provider_id)
        validate_id(server_id)
        return self._compute.get_server(ctx, server_id)

    def create_server(self, ctx: RequestContext, spec: CreateServerSpec) -> Accepted:
        validate_context(ctx, self._project_id, self._provider_id)
        normalized = normalize_create_spec(spec)
        self._catalog.validate_create_references(ctx, normalized)
        return self._compute.create_server(ctx, normalized)

    def delete_server(self, ctx: RequestContext, server_id: str) -> DeleteResult:
        validate_context(ctx, self._project_id, self._provider_id)
        validate_id(server_id)
        return self._compute.delete_server(ctx, server_id)

    def act_server(self, ctx: RequestContext, server_id: str, action: ServerAction) -> Accepted:
        validate_context(ctx, self._project_id, self._provider_id)
        validate_id(server_id)
        if not isinstance(action, str) or action not in ("start", "stop", "reboot"):
            raise InvalidInput("지원하지 않는 서버 작업입니다.")
        return self._compute.act_server(ctx, server_id, action)
