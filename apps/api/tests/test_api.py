from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "echotrace-api"
    assert len(response.json()["runtime_fingerprint"]) == 20


def test_private_endpoint_requires_login() -> None:
    response = TestClient(app).get("/moments")
    assert response.status_code == 401
