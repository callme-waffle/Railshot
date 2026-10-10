from control_plane.application.validation import validate_context, validate_page
from control_plane.domain.models import Flavor, Image, Network, Page, PageRequest, RequestContext
from control_plane.ports.catalog import CatalogProvider


class CatalogService:
    def __init__(self, catalog: CatalogProvider, project_id: str, provider_id: str) -> None:
        self._catalog = catalog
        self._project_id = project_id
        self._provider_id = provider_id

    def list_images(self, ctx: RequestContext, page: PageRequest) -> Page[Image]:
        validate_context(ctx, self._project_id, self._provider_id)
        validate_page(page)
        return self._catalog.list_images(ctx, page)

    def list_flavors(self, ctx: RequestContext, page: PageRequest) -> Page[Flavor]:
        validate_context(ctx, self._project_id, self._provider_id)
        validate_page(page)
        return self._catalog.list_flavors(ctx, page)

    def list_networks(self, ctx: RequestContext, page: PageRequest) -> Page[Network]:
        validate_context(ctx, self._project_id, self._provider_id)
        validate_page(page)
        return self._catalog.list_networks(ctx, page)
