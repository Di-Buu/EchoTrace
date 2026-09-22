from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "echotrace-api"}


def test_private_endpoint_requires_login() -> None:
    response = TestClient(app).get("/moments")
    assert response.status_code == 401
