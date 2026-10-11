from keystoneauth1 import exceptions as identity_errors
from openstack.connection import Connection

from control_plane.domain.errors import Forbidden, UpstreamFailure
from control_plane.domain.models import RequestContext
from control_plane.infrastructure.openstack.errors import upstream


class ScopedAdapter:
    def __init__(self, connection: Connection, project_id: str, provider_id: str) -> None:
        self.connection = connection
        self.project_id = project_id
        self.provider_id = provider_id

    def check_scope(self, ctx: RequestContext) -> None:
        if ctx.project_id != self.project_id or ctx.provider_id != self.provider_id:
            raise Forbidden()
        with upstream():
            try:
                authenticated_project = self.connection.session.get_project_id()
            except identity_errors.HttpError as exc:
                if exc.http_status in (400, 401, 403):
                    raise UpstreamFailure("외부 인증 설정을 확인해 주세요.") from exc
                raise
            if authenticated_project != self.project_id:
                raise UpstreamFailure("외부 인증 프로젝트가 설정과 일치하지 않습니다.")
