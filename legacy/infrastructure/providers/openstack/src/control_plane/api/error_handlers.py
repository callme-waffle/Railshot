"""Convert all public failures to the same safe HTTP envelope."""

from collections.abc import Mapping
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from control_plane.api.schemas import ErrorDetail, ErrorResponse
from control_plane.domain.errors import (
    ControlPlaneError,
    Forbidden,
    InternalFailure,
    InvalidInput,
    MethodNotAllowed,
    NotFound,
    Unauthenticated,
)

STATUS_CODES = {
    "INVALID_INPUT": 422,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "CONFLICT": 409,
    "QUOTA_EXCEEDED": 409,
    "RATE_LIMITED": 429,
    "UPSTREAM_FAILURE": 502,
    "UPSTREAM_UNAVAILABLE": 503,
    "UPSTREAM_TIMEOUT": 504,
    "INTERNAL_ERROR": 500,
}


def error_response(
    error: ControlPlaneError,
    request_id: str,
    status_code: int | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    response_headers = dict(headers or {})
    response_headers["X-Request-ID"] = request_id
    if isinstance(error, Unauthenticated):
        response_headers["WWW-Authenticate"] = "Bearer"
    body = ErrorResponse(
        error=ErrorDetail(
            code=error.code,
            message=error.message,
            request_id=request_id,
            retryable=error.retryable,
            outcome_unknown=error.outcome_unknown,
        )
    )
    return JSONResponse(
        status_code=status_code or STATUS_CODES.get(error.code, 500),
        content=body.model_dump(),
        headers=response_headers,
    )


def _request_id(request: Request) -> str:
    value: object = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else str(uuid4())


async def control_plane_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ControlPlaneError)
    return error_response(exc, _request_id(request))


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Validation errors can contain entire request bodies, credentials and reprs.
    return error_response(InvalidInput(), _request_id(request))


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    errors: dict[int, type[ControlPlaneError]] = {
        400: InvalidInput,
        401: Unauthenticated,
        403: Forbidden,
        404: NotFound,
        405: MethodNotAllowed,
        422: InvalidInput,
    }
    error = errors.get(exc.status_code, InternalFailure)()
    return error_response(error, _request_id(request), exc.status_code, exc.headers)


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return error_response(InternalFailure(), _request_id(request))


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ControlPlaneError, control_plane_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(Exception, internal_error_handler)
