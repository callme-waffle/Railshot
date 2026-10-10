from __future__ import annotations

from fastapi import FastAPI

from control_plane.auth import RequestContextMiddleware
from control_plane.bootstrap import ProviderBundle
from control_plane.config import Settings


def create_app(
    settings: Settings | None = None, providers: ProviderBundle | None = None
) -> FastAPI:
    from control_plane.api.error_handlers import register_error_handlers
    from control_plane.api.router import router
    from control_plane.api.schemas import ErrorResponse

    if settings is None:
        settings = Settings()  # type: ignore[call-arg]  # BaseSettings reads environment.
    if providers is None:
        settings.validate_openstack()
    app = FastAPI(
        title="OpenStack Control Plane",
        version="0.1.0",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        responses={
            status: {"model": ErrorResponse}
            for status in (401, 403, 404, 405, 409, 422, 429, 500, 502, 503, 504)
        },
    )
    app.state.settings = settings
    app.state.providers = providers
    register_error_handlers(app)
    app.include_router(router)
    app.add_middleware(RequestContextMiddleware, settings=settings)
    return app
