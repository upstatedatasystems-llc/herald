from fastapi.testclient import TestClient

from apps.api.main import app
from packages.herald.config import settings

client = TestClient(app)


def test_api_key_required_in_production(monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "production")
    monkeypatch.setattr(settings, "HERALD_API_KEY", "secret-test-key-123")

    # Missing API key -> 401
    res = client.post("/api/v1/extract", json={"url": "https://example.com"})
    assert res.status_code == 401

    # Wrong API key -> 403
    res_wrong = client.post(
        "/api/v1/extract",
        json={"url": "https://example.com"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert res_wrong.status_code == 403

    # Correct API key -> Not 401/403
    res_correct = client.post(
        "/api/v1/extract",
        json={"url": "https://example.com"},
        headers={"X-API-Key": "secret-test-key-123"},
    )
    assert res_correct.status_code not in (401, 403)
