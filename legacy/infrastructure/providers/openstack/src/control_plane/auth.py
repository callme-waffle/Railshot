from __future__ import annotations

import logging
import secrets
from time import monotonic
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from control_plane.config import Settings
from control_plane.domain.errors import InternalFailure, Unauthenticated
from control_plane.domain.models import RequestContext

logger = logging.getLogger("control_plane.requests")


class RequestContextMiddleware:
    """Authenticate before body parsing; generate an ID even for rejected requests."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from control_plane.api.error_handlers import error_response

        request_id = str(uuid4())
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        started = monotonic()
        status = 500
        response_started = False

        async def traced_send(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        public_paths = {"/health/live"}
        if self.settings.docs_enabled:
            public_paths.update({"/docs", "/openapi.json", "/docs/oauth2-redirect"})
        try:
            if scope["path"] not in public_paths:
                values = Headers(scope=scope).getlist("authorization")
                scheme, _, credential = (
                    values[0].partition(" ") if len(values) == 1 else ("", "", "")
                )
                if scheme.lower() != "bearer" or not secrets.compare_digest(
                    credential.encode(), self.settings.api_token.get_secret_value().encode()
                ):
                    await error_response(
                        Unauthenticated(), request_id, headers={"WWW-Authenticate": "Bearer"}
                    )(scope, receive, traced_send)
                    return
                state["context"] = RequestContext(
                    request_id=request_id,
                    principal_id=self.settings.principal_id,
                    project_id=self.settings.project_id,
                    provider_id=self.settings.provider_id,
                )
            await self.app(scope, receive, traced_send)
        except Exception:
            # Do not log raw exception text: SDK errors can contain credentials or bodies.
            logger.error("request_failed request_id=%s", request_id)
            if not response_started:
                await error_response(InternalFailure(), request_id)(scope, receive, traced_send)
        finally:
            route = scope.get("route")
            logger.info(
                "request request_id=%s principal=%s project=%s provider=%s method=%s "
                "route=%s status=%s duration_ms=%.1f",
                request_id,
                self.settings.principal_id if "context" in state else "anonymous",
                self.settings.project_id,
                self.settings.provider_id,
                scope["method"],
                getattr(route, "path", "unmatched"),
                status,
                (monotonic() - started) * 1000,
            )
