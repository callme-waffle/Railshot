from control_plane.application.validation import validate_context
from control_plane.domain.errors import (
    RateLimited,
    UpstreamFailure,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from control_plane.domain.models import RequestContext
from control_plane.ports.health import HealthProvider


class HealthService:
    def __init__(self, health: HealthProvider, project_id: str, provider_id: str) -> None:
        self._health = health
        self._project_id = project_id
        self._provider_id = provider_id

    def check_ready(self, ctx: RequestContext) -> bool:
        validate_context(ctx, self._project_id, self._provider_id)
        try:
            return self._health.check_ready(ctx)
        except (UpstreamFailure, UpstreamUnavailable, UpstreamTimeout, RateLimited):
            return False
