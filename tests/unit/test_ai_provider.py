import httpx
import pytest
from herald.ai.factory import get_ai_provider
from herald.ai.gemini_provider import GeminiProvider
from herald.config import settings
from herald.core.models import HeraldRequest
from herald.core.pipeline import process_herald_request
from herald.db.models import JobState


def test_gemini_health_check_success(monkeypatch):
    """Point 13: Gemini health check success."""
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "valid-secret-key-12345")
    monkeypatch.setattr(settings, "AI_PROVIDER", "gemini")

    def mock_get(self, url, **kwargs):
        assert "key=" not in url
        headers = kwargs.get("headers", {})
        assert headers.get("x-goog-api-key") == "valid-secret-key-12345"
        return httpx.Response(200, json={"name": "models/gemini-3.5-flash", "displayName": "Gemini 3.5 Flash"})

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    provider = GeminiProvider()
    res = provider.check_connection()
    assert res["configured"] is True
    assert res["connected"] is True
    assert res["error"] is None
    assert res["provider"] == "Gemini"


def test_gemini_health_check_auth_failure_sanitized(monkeypatch):
    """Point 14 & 15: Gemini health check auth failure and secret sanitization."""
    secret_key = "super-confidential-api-key-999"
    monkeypatch.setattr(settings, "GEMINI_API_KEY", secret_key)
    monkeypatch.setattr(settings, "AI_PROVIDER", "gemini")

    def mock_get(self, url, **kwargs):
        return httpx.Response(403, text=f"API_KEY_INVALID: Key {secret_key} was rejected.")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    provider = GeminiProvider()
    res = provider.check_connection()
    assert res["configured"] is True
    assert res["connected"] is False
    assert res["error"] == "authentication failed"
    # Ensure raw secret key is not in error
    assert secret_key not in str(res)


def test_ai_mode_rejected_when_provider_absent(db_session, monkeypatch):
    """Point 11: AI mode rejected clearly when provider is absent."""
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "AI_PROVIDER", "none")

    req = HeraldRequest(
        source_text="Some article text about astronomy and stars.",
        request_mode="standard",
        requester_identity="telegram:12345",
        delivery_target="12345",
        transport="telegram",
        transport_message_id="1",
    )

    resp = process_herald_request(db_session, req)
    assert resp.status == JobState.FAILED_FINAL.value
    assert "AI provider is not configured" in resp.message
    assert "literal" in resp.message.lower()


def test_literal_usable_when_ai_provider_unreachable(db_session, monkeypatch):
    """Point 12: Literal remains usable when configured AI provider is unreachable."""
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "real-key-but-api-offline")
    monkeypatch.setattr(settings, "AI_PROVIDER", "gemini")

    # Mock Gemini call to fail with network error
    def mock_gemini_error(*args, **kwargs):
        raise httpx.ConnectError("Network unreachable")

    monkeypatch.setattr("herald.gemini.client.generate_podcast_script", mock_gemini_error)

    req = HeraldRequest(
        source_text="# Resilient Engineering\n\nSystems must survive upstream provider outages.",
        request_mode="literal",
        requester_identity="telegram:12345",
        delivery_target="12345",
        transport="telegram",
        transport_message_id="2",
    )

    resp = process_herald_request(db_session, req)
    assert resp.status == JobState.QUEUED_TTS.value
    assert resp.request_mode == "literal"
    assert resp.episode_title == "Resilient Engineering"
