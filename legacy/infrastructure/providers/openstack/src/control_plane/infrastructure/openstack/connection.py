"""Explicit Keystone credentials; never load clouds.yaml or ambient OS_* settings."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from inspect import signature
from typing import Any
from uuid import UUID

from keystoneauth1.identity import v3
from keystoneauth1.plugin import BaseAuthPlugin
from keystoneauth1.session import Session
from openstack import exceptions as sdk_errors
from openstack.config import defaults
from openstack.config.cloud_region import CloudRegion
from openstack.connection import Connection

from control_plane.config import Settings

_REQUEST_SIGNATURE = signature(Session.request)
logger = logging.getLogger("control_plane.upstream")


class ControllerSession(Session):
    """Enforce bounded requests and forbid implicit replay of cloud mutations."""

    def __init__(
        self, *, connect_timeout: float, read_timeout: float, ready_timeout: float, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.request_timeout = (connect_timeout, read_timeout)
        self.ready_timeout = ready_timeout
        self.request_id: str | None = None

    def request(self, url: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if args:
            bound = _REQUEST_SIGNATURE.bind(self, url, method, *args, **kwargs)
            values = dict(bound.arguments)
            kwargs = values.pop("kwargs", {})
            for key in ("self", "url", "method"):
                values.pop(key)
            kwargs.update(values)
        kwargs["timeout"] = self.request_timeout
        kwargs["connect_retries"] = 0
        kwargs["status_code_retries"] = 0
        kwargs["log"] = False
        if method.upper() not in ("GET", "HEAD", "OPTIONS"):
            kwargs["allow_reauth"] = False
            # 307/308 may replay POST/DELETE, so do not follow mutation redirects.
            kwargs["redirect"] = False
        response = None
        try:
            response = super().request(url, method, **kwargs)
        except Exception as exc:
            response = getattr(exc, "response", None)
            raise
        finally:
            if self.request_id is not None:
                upstream_id = None
                if response is not None:
                    candidate = response.headers.get("X-Openstack-Request-Id", "")
                    if isinstance(candidate, str) and candidate.startswith("req-"):
                        try:
                            upstream_id = f"req-{UUID(candidate[4:])}"
                        except ValueError:
                            pass
                logger.info(
                    "upstream request_id=%s method=%s status=%s upstream_request_id=%s",
                    self.request_id,
                    method.upper(),
                    getattr(response, "status_code", None),
                    upstream_id,
                )
        if method.upper() not in ("GET", "HEAD", "OPTIONS") and 300 <= response.status_code < 400:
            raise sdk_errors.HttpException("Unexpected mutation redirect", response=response)
        return response


@contextmanager
def open_connection(settings: Settings) -> Iterator[Connection]:
    settings.validate_openstack()
    assert settings.auth_url is not None
    auth: BaseAuthPlugin
    if settings.auth_type == "password":
        assert settings.password is not None
        auth = v3.Password(
            auth_url=settings.auth_url,
            username=settings.username,
            password=settings.password.get_secret_value(),
            user_domain_name=settings.user_domain_name,
            project_id=settings.project_id,
        )
    else:
        assert settings.application_credential_secret is not None
        auth = v3.ApplicationCredential(
            auth_url=settings.auth_url,
            application_credential_id=settings.application_credential_id,
            application_credential_secret=settings.application_credential_secret.get_secret_value(),
        )
    session = ControllerSession(
        auth=auth,
        verify=settings.ca_cert or True,
        connect_timeout=settings.connect_timeout,
        read_timeout=settings.read_timeout,
        ready_timeout=settings.ready_timeout,
    )
    connection: Connection | None = None
    try:
        config = CloudRegion(
            session=session,
            region_name=settings.region_name,
            config={
                **defaults.get_defaults(),
                "interface": settings.interface,
                "compute_api_version": "2",
                "image_api_version": "2",
                "network_api_version": "2",
                "connect_retries": 0,
                "status_code_retries": 0,
            },
        )
        connection = Connection(config=config)
        yield connection
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            # SDK Connection.close() only closes executors/cache, not HTTP pool.
            session.session.close()
