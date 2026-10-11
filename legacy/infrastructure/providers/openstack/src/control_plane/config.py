from __future__ import annotations

from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_", extra="ignore", hide_input_in_errors=True)

    api_token: SecretStr
    project_id: str = Field(min_length=1)
    provider_id: str = Field(default="openstack-main", min_length=1)
    principal_id: str = Field(default="operator", min_length=1)
    docs_enabled: bool = False
    auth_url: str | None = None
    auth_type: Literal["password", "application_credential"] = "password"
    username: str | None = None
    password: SecretStr | None = None
    user_domain_name: str | None = None
    application_credential_id: str | None = None
    application_credential_secret: SecretStr | None = None
    region_name: str | None = None
    interface: Literal["public", "internal"] = "public"
    ca_cert: str | None = None
    connect_timeout: float = Field(default=5, gt=0, le=60)
    read_timeout: float = Field(default=20, gt=0, le=120)
    ready_timeout: float = Field(default=3, gt=0, le=30)

    @model_validator(mode="after")
    def validate_common(self) -> Self:
        if not self.api_token.get_secret_value().strip():
            raise ValueError("CP_API_TOKEN must not be blank")
        for value in (self.project_id, self.provider_id, self.principal_id):
            if not value.strip() or value != value.strip():
                raise ValueError(
                    "Identity settings must be nonblank without surrounding whitespace"
                )
        return self

    def validate_openstack(self) -> None:
        """Called for real providers only; injected test providers need no cloud credentials."""
        parsed = urlparse(self.auth_url or "")
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("CP_AUTH_URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("CP_AUTH_URL must not contain credentials, query, or fragment")
        if self.auth_type == "password":
            if not all((self.username, self.password, self.user_domain_name)):
                raise ValueError(
                    "Password authentication requires username, password and user domain"
                )
            if self.application_credential_id or self.application_credential_secret:
                raise ValueError("Do not mix authentication methods")
        else:
            if not all((self.application_credential_id, self.application_credential_secret)):
                raise ValueError("Application credential authentication requires ID and secret")
            if self.username or self.password or self.user_domain_name:
                raise ValueError("Do not mix authentication methods")
        for secret in (self.password, self.application_credential_secret):
            if secret is not None and not secret.get_secret_value().strip():
                raise ValueError("Authentication secrets must not be blank")
