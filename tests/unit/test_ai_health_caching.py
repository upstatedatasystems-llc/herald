import time
import httpx
from unittest.mock import MagicMock
from herald.ai.gemini_provider import GeminiProvider
from herald.config import settings


def test_ai_health_caching_and_force_refresh(monkeypatch):
    """
    Test that:
    1. check_connection(force_refresh=False) caches the result for 5 minutes.
    2. Repeated calls do not invoke HTTP client.
    3. check_connection(force_refresh=True) bypasses cache and makes a fresh request.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key-12345")
    monkeypatch.setattr(settings, "AI_PROVIDER", "gemini")

    call_count = 0

    def mock_get(self, url, **kwargs):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"name": "models/gemini-3.5-flash"})

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    provider = GeminiProvider(cache_ttl_seconds=300.0)

    # First call - fresh
    res1 = provider.check_connection(force_refresh=False)
    assert res1["connected"] is True
    assert call_count == 1

    # Second call - cached
    res2 = provider.check_connection(force_refresh=False)
    assert res2["connected"] is True
    assert call_count == 1  # No additional HTTP call

    # Third call with force_refresh=True - bypasses cache
    res3 = provider.check_connection(force_refresh=True)
    assert res3["connected"] is True
    assert call_count == 2  # New HTTP call triggered
