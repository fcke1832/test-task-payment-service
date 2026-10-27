from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


def test_health_requires_api_key() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 401


def test_health_accepts_valid_api_key() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/health",
            headers={"X-API-Key": settings.API_KEY},
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_rejects_invalid_api_key() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/health",
            headers={"X-API-Key": "definitely-wrong"},
        )
    assert response.status_code == 401
