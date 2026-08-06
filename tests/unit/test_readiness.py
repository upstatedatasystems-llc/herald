from fastapi.testclient import TestClient

from apps.api.main import app
from herald.config import settings

client = TestClient(app)


def test_readiness_fails_503_when_kokoro_required_and_unhealthy(monkeypatch):
    monkeypatch.setenv("HERALD_REQUIRE_KOKORO", "1")
    monkeypatch.setattr(settings, "HERALD_ENV", "production")

    res = client.get("/readiness")
    assert res.status_code == 503
    data = res.json()
    assert "detail" in data
    assert data["detail"]["ready"] is False
    assert any("Kokoro" in r or "Production" in r for r in data["detail"]["reasons"])


def test_liveness_succeeds_even_when_dependencies_fail(monkeypatch):
    monkeypatch.setenv("HERALD_REQUIRE_KOKORO", "1")

    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "live"
