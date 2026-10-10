import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from control_plane.config import Settings
from control_plane.main import create_app
from tests.fakes.providers import FakeCloud


def test_no_credentials_required_for_injected_providers():
    settings = Settings(api_token="test-token", project_id="project-test")
    assert create_app(settings, FakeCloud().bundle())
    with pytest.raises(ValueError, match="AUTH_URL"):
        create_app(settings)


def test_opt_in_docs_describe_auth_and_error_contract():
    settings = Settings(api_token="test-token", project_id="project-test", docs_enabled=True)
    with TestClient(create_app(settings, FakeCloud().bundle())) as client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
        operation = schema["paths"]["/api/v1/servers"]["post"]
        assert operation["security"] == [{"HTTPBearer": []}]
        assert operation["responses"]["422"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
        assert client.get("/api/v1/servers").status_code == 401


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_token": ""},
        {"api_token": "  "},
        {"project_id": " "},
        {"project_id": " project "},
        {"connect_timeout": 0},
        {"interface": "admin"},
    ],
)
def test_invalid_common_settings(kwargs):
    with pytest.raises(ValidationError):
        Settings(**({"api_token": "test-token", "project_id": "project-test"} | kwargs))


def test_secret_repr_and_error_do_not_expose_values():
    config = Settings(api_token="private-token", project_id="project-test", password="private-pass")
    assert "private-token" not in repr(config)
    assert "private-pass" not in repr(config)
    with pytest.raises(ValidationError) as caught:
        Settings(api_token="private-token", project_id="project-test", connect_timeout=-1)
    assert "private-token" not in str(caught.value)


def test_password_and_application_credentials_are_exclusive():
    base = dict(api_token="test", project_id="project-test", auth_url="https://cloud.invalid/v3")
    password = Settings(**base, username="user", password="secret", user_domain_name="Default")
    password.validate_openstack()
    credential = Settings(
        **base,
        auth_type="application_credential",
        application_credential_id="id",
        application_credential_secret="secret",
    )
    credential.validate_openstack()
    with pytest.raises(ValueError, match="mix"):
        password.model_copy(update={"application_credential_id": "id"}).validate_openstack()


@pytest.mark.parametrize(
    "auth_url",
    [
        "bad",
        "ftp://cloud.invalid",
        "https://user:secret@cloud.invalid",
        "https://cloud.invalid/v3?secret=secret",
    ],
)
def test_invalid_auth_url(auth_url):
    with pytest.raises(ValueError):
        Settings(
            api_token="test", project_id="project-test", auth_url=auth_url
        ).validate_openstack()
