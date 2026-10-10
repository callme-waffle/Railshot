from __future__ import annotations


class ControlPlaneError(Exception):
    code = "INTERNAL_ERROR"
    default_message = "내부 오류가 발생했습니다."

    def __init__(
        self, message: str | None = None, *, retryable: bool = False, outcome_unknown: bool = False
    ) -> None:
        self.message = message if message is not None else self.default_message
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        super().__init__(self.message)


class InvalidInput(ControlPlaneError):
    code = "INVALID_INPUT"
    default_message = "입력값이 올바르지 않습니다."


class Unauthenticated(ControlPlaneError):
    code = "UNAUTHENTICATED"
    default_message = "인증이 필요합니다."


class Forbidden(ControlPlaneError):
    code = "FORBIDDEN"
    default_message = "허용되지 않은 작업입니다."


class NotFound(ControlPlaneError):
    code = "NOT_FOUND"
    default_message = "자원을 찾을 수 없습니다."


class MethodNotAllowed(ControlPlaneError):
    code = "METHOD_NOT_ALLOWED"
    default_message = "지원하지 않는 HTTP 메서드입니다."


class Conflict(ControlPlaneError):
    code = "CONFLICT"
    default_message = "현재 자원 상태에서 수행할 수 없습니다."


class QuotaExceeded(ControlPlaneError):
    code = "QUOTA_EXCEEDED"
    default_message = "프로젝트 할당량을 초과했습니다."


class RateLimited(ControlPlaneError):
    code = "RATE_LIMITED"
    default_message = "요청 빈도 제한을 초과했습니다."


class UpstreamFailure(ControlPlaneError):
    code = "UPSTREAM_FAILURE"
    default_message = "외부 서비스 응답을 처리할 수 없습니다."


class UpstreamUnavailable(ControlPlaneError):
    code = "UPSTREAM_UNAVAILABLE"
    default_message = "외부 서비스를 사용할 수 없습니다."


class UpstreamTimeout(ControlPlaneError):
    code = "UPSTREAM_TIMEOUT"
    default_message = "외부 서비스 응답 시간이 초과되었습니다."


class InternalFailure(ControlPlaneError):
    code = "INTERNAL_ERROR"
    default_message = "내부 오류가 발생했습니다."
