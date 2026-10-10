from itertools import islice

from control_plane.domain.models import RequestContext
from control_plane.infrastructure.openstack.base import ScopedAdapter
from control_plane.infrastructure.openstack.connection import ControllerSession
from control_plane.infrastructure.openstack.errors import upstream


class OpenStackHealthAdapter(ScopedAdapter):
    def check_ready(self, ctx: RequestContext) -> bool:
        session = self.connection.session
        old_timeout = None
        if isinstance(session, ControllerSession):
            old_timeout = session.request_timeout
            session.request_timeout = (
                min(old_timeout[0], session.ready_timeout),
                min(old_timeout[1], session.ready_timeout),
            )
        try:
            self.check_scope(ctx)
            with upstream():
                # Read one authorized object (or empty page) from each required service.
                tuple(
                    islice(
                        self.connection.compute.servers(
                            details=True, project_id=self.project_id, limit=1, max_items=1
                        ),
                        1,
                    )
                )
                tuple(islice(self.connection.image.images(limit=1, max_items=1), 1))
                tuple(islice(self.connection.network.networks(limit=1, max_items=1), 1))
            return True
        finally:
            if isinstance(session, ControllerSession) and old_timeout is not None:
                session.request_timeout = old_timeout
