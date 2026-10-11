"""Translate SDK errors without exposing upstream messages or credentials."""

from collections.abc import Iterator
from contextlib import contextmanager
from json import JSONDecodeError

from keystoneauth1 import exceptions as identity_errors
from openstack import exceptions as sdk_errors

from control_plane.domain.errors import (
    Conflict,
    ControlPlaneError,
    Forbidden,
    InvalidInput,
    NotFound,
    QuotaExceeded,
    RateLimited,
    UpstreamFailure,
    UpstreamTimeout,
    UpstreamUnavailable,
)


def normalize(exc: Exception, *, mutation: bool) -> ControlPlaneError:
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    # Nova uses 403 overLimit; recognize explicit quota evidence, not all 403s.
    details = str(getattr(exc, "details", "") or "").lower()
    if status in (403, 409, 413) and any(
        phrase in details for phrase in ("quota exceeded", "quota exceeded for", "exceeds quota")
    ):
        return QuotaExceeded()
    if isinstance(exc, (TimeoutError, identity_errors.ConnectTimeout)):
        return UpstreamTimeout(retryable=not mutation, outcome_unknown=mutation)
    # Keystone wraps requests ReadTimeout in RetriableConnectionFailure.
    cause = exc.__cause__ or exc.__context__
    if isinstance(cause, Exception) and "timeout" in type(cause).__name__.lower():
        return UpstreamTimeout(retryable=not mutation, outcome_unknown=mutation)
    if isinstance(exc, identity_errors.ConnectFailure):
        return UpstreamUnavailable(retryable=not mutation, outcome_unknown=mutation)
    if isinstance(
        exc,
        (
            identity_errors.EndpointNotFound,
            identity_errors.DiscoveryFailure,
            sdk_errors.ServiceDiscoveryException,
            sdk_errors.ServiceDisabledException,
        ),
    ):
        return UpstreamUnavailable(retryable=not mutation)
    if status == 400:
        return InvalidInput()
    if status == 403:
        return Forbidden()
    if status == 404 or isinstance(exc, sdk_errors.NotFoundException):
        return NotFound()
    if status == 409:
        return Conflict()
    if status == 429:
        return RateLimited(retryable=not mutation)
    if status in (502, 503, 504):
        cls = UpstreamTimeout if status == 504 else UpstreamUnavailable
        return cls(retryable=not mutation, outcome_unknown=mutation)
    return UpstreamFailure(outcome_unknown=mutation and (status is None or status >= 500))


@contextmanager
def upstream(*, mutation: bool = False) -> Iterator[None]:
    try:
        yield
    except ControlPlaneError:
        raise
    except (
        sdk_errors.SDKException,
        identity_errors.ClientException,
        TimeoutError,
        JSONDecodeError,
    ) as exc:
        raise normalize(exc, mutation=mutation) from exc
