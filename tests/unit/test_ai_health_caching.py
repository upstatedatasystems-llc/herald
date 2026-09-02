import time
import httpx
from unittest.mock import MagicMock

from herald.ai.factory import get_ai_provider, reset_ai_provider
from herald.config import settings


def test_ai_health_caching_persistent_across_get_ai_provider_calls(monkeypatch):
    """
    Test Requirement 4:
    1. Repeated get_ai_provider().check_connection(force_refresh=False) inside cache TTL -> 1 provider HTTP request.
    2. /ai-check with force_refresh=True -> forces a fresh request.
    3. Cache expiration -> next check_connection refreshes.
    4. Literal operation (AI_PROVIDER=none) returns None independently.
    """
    reset_ai_provider()
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key-12345")
    monkeypatch.setattr(settings, "AI_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")

    call_count = 0

    def mock_get(self, url, **kwargs):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"name": "models/gemini-3.5-flash"})

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    # 1. First call (e.g. /status) -> fresh HTTP request
    provider1 = get_ai_provider()
    assert provider1 is not None
    res1 = provider1.check_connection(force_refresh=False)
    assert res1["connected"] is True
    assert call_count == 1

    # 2. Second call from separate get_ai_provider() invocation (e.g. next /status) -> cached, 0 HTTP requests
    provider2 = get_ai_provider()
    assert provider2 is not None
    res2 = provider2.check_connection(force_refresh=False)
    assert res2["connected"] is True
    assert call_count == 1

    # 3. Third call with force_refresh=True (e.g. /ai-check) -> forces fresh request
    provider3 = get_ai_provider()
    res3 = provider3.check_connection(force_refresh=True)
    assert res3["connected"] is True
    assert call_count == 2

    # 4. Cache expiration test
    provider3._cache_timestamp = time.time() - 400.0  # simulate > 300s TTL passed
    provider4 = get_ai_provider()
    res4 = provider4.check_connection(force_refresh=False)
    assert res4["connected"] is True
    assert call_count == 3

    # 5. Literal mode operation independent
    monkeypatch.setattr(settings, "AI_PROVIDER", "none")
    assert get_ai_provider() is None

    reset_ai_provider()
