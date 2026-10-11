"""Framework-independent validation shared by the application services."""

from dataclasses import replace

from control_plane.domain.errors import Forbidden, InvalidInput
from control_plane.domain.models import CreateServerSpec, PageRequest, RequestContext


def validate_context(ctx: RequestContext, project_id: str, provider_id: str) -> None:
    if (
        not isinstance(ctx, RequestContext)
        or not project_id
        or not provider_id
        or ctx.project_id != project_id
        or ctx.provider_id != provider_id
    ):
        raise Forbidden()


def validate_id(value: str) -> None:
    # Provider identifiers are opaque; validate without rewriting or imposing UUIDs.
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput("자원 ID는 비어 있지 않은 문자열이어야 합니다.")


def validate_page(page: PageRequest) -> None:
    if (
        not isinstance(page, PageRequest)
        or type(page.limit) is not int
        or not 1 <= page.limit <= 100
    ):
        raise InvalidInput("조회 개수는 1~100 사이의 정수여야 합니다.")
    if page.marker is not None:
        validate_id(page.marker)


def normalize_create_spec(spec: CreateServerSpec) -> CreateServerSpec:
    if not isinstance(spec, CreateServerSpec) or not isinstance(spec.name, str):
        raise InvalidInput("서버 생성 입력이 올바르지 않습니다.")
    name = spec.name.strip()
    if not 1 <= len(name) <= 255:
        raise InvalidInput("서버 이름은 양끝 공백을 제외하고 1~255자여야 합니다.")
    validate_id(spec.image_id)
    validate_id(spec.flavor_id)
    if not isinstance(spec.network_ids, tuple) or not spec.network_ids:
        raise InvalidInput("연결할 네트워크를 하나 이상 지정해야 합니다.")
    for network_id in spec.network_ids:
        validate_id(network_id)
    if len(set(spec.network_ids)) != len(spec.network_ids):
        raise InvalidInput("네트워크 ID는 중복될 수 없습니다.")
    return replace(spec, name=name)
