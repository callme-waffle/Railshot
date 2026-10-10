"""Local-only demonstration with in-memory providers and a fixed non-secret test token."""

from fastapi import FastAPI

from control_plane.config import Settings
from control_plane.main import create_app as build_app
from tests.fakes.providers import FakeCloud


def create_app() -> FastAPI:
    return build_app(
        Settings(api_token="local-demo-token", project_id="demo-project", docs_enabled=True),
        FakeCloud().bundle(),
    )
