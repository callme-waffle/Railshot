from fastapi.testclient import TestClient

from app import app


def test_health_returns_application_result() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
