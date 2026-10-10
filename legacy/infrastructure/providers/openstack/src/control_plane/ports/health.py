from __future__ import annotations

from typing import Protocol

from control_plane.domain.models import (
    RequestContext,
)


class HealthProvider(Protocol):
    def check_ready(self, ctx: RequestContext) -> bool: ...
