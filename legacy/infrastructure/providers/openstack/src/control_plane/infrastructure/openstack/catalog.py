from control_plane.domain.errors import InvalidInput, NotFound, UpstreamFailure
from control_plane.domain.models import (
    CreateServerSpec,
    Flavor,
    Image,
    Network,
    Page,
    PageRequest,
    RequestContext,
)
from control_plane.infrastructure.openstack import mapping
from control_plane.infrastructure.openstack.base import ScopedAdapter
from control_plane.infrastructure.openstack.errors import upstream


class OpenStackCatalogAdapter(ScopedAdapter):
    def list_images(self, ctx: RequestContext, page: PageRequest) -> Page[Image]:
        self.check_scope(ctx)
        with upstream():
            return mapping.page_result(
                self.connection.image.images(**mapping.page_query(page)), page, mapping.image
            )

    def list_flavors(self, ctx: RequestContext, page: PageRequest) -> Page[Flavor]:
        self.check_scope(ctx)
        with upstream():
            return mapping.page_result(
                self.connection.compute.flavors(
                    details=True, get_extra_specs=False, **mapping.page_query(page)
                ),
                page,
                mapping.flavor,
            )

    def list_networks(self, ctx: RequestContext, page: PageRequest) -> Page[Network]:
        self.check_scope(ctx)
        with upstream():
            # Neutron authorizes shared/RBAC-accessible networks; ownership alone is not access.
            return mapping.page_result(
                self.connection.network.networks(**mapping.page_query(page)), page, mapping.network
            )

    def validate_create_references(self, ctx: RequestContext, spec: CreateServerSpec) -> None:
        self.check_scope(ctx)
        try:
            with upstream():
                image = self.connection.image.get_image(spec.image_id)
                if mapping.text_field(image, "id") != spec.image_id:
                    raise UpstreamFailure()
                if mapping.text_field(image, "status", default="UNKNOWN").upper() != "ACTIVE":
                    raise InvalidInput("사용 가능한 활성 이미지를 지정해 주세요.")
                flavor = self.connection.compute.get_flavor(spec.flavor_id)
                if mapping.text_field(flavor, "id") != spec.flavor_id:
                    raise UpstreamFailure()
                for network_id in spec.network_ids:
                    network = self.connection.network.get_network(network_id)
                    if mapping.text_field(network, "id") != network_id:
                        raise UpstreamFailure()
        except NotFound as exc:
            raise InvalidInput("이미지·사양·네트워크 참조를 확인해 주세요.") from exc
