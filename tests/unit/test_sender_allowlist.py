from fastapi.testclient import TestClient

from apps.api.main import app
from herald.config import settings

client = TestClient(app)


def test_sender_allowlist_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "production")
    monkeypatch.setattr(settings, "HERALD_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EMAIL_ALLOWED_SENDERS", "")

    req_payload = {
        "gmail_message_id": "msg-allowlist-1",
        "sender_email": "anyone@example.com",
        "subject": "Podcast: Standard",
        "body_text": "Sample valid content body for intake test.",
    }

    # Empty allowlist in production -> 403 Forbidden
    res = client.post(
        "/api/v1/intake",
        json=req_payload,
        headers={"X-API-Key": "test-key"},
    )
    assert res.status_code == 403
    assert "allowlist is empty" in res.json()["detail"]


def test_unauthorized_sender_rejected(monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "production")
    monkeypatch.setattr(settings, "HERALD_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EMAIL_ALLOWED_SENDERS", "allowed@example.com")

    req_payload = {
        "gmail_message_id": "msg-allowlist-2",
        "sender_email": "unauthorized@example.com",
        "subject": "Podcast: Standard",
        "body_text": "Sample valid content body for intake test.",
    }

    res = client.post(
        "/api/v1/intake",
        json=req_payload,
        headers={"X-API-Key": "test-key"},
    )
    assert res.status_code == 403
    assert "not authorized" in res.json()["detail"]
