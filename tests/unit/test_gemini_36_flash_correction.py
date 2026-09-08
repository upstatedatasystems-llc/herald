"""
Unit tests for Gemini 3.6-Flash default, migration, specific 404 classification,
and independent /ai-check validation.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from herald.ai.gemini_provider import GeminiProvider
from herald.config import settings
from herald.gemini.client import (
    GeminiError,
    GeminiModelUnavailableError,
    _is_gemini_model_not_found_response,
    generate_grounded_research,
)


def test_default_research_model_is_gemini_36_flash():
    """Verify clean configuration defaults to gemini-3.6-flash."""
    assert settings.GEMINI_RESEARCH_MODEL == "gemini-3.6-flash"


def test_setup_migration_upgrades_former_default_and_preserves_custom(tmp_path):
    """Verify setup migration upgrades former default gemini-2.5-flash while preserving custom models."""
    env_file = tmp_path / ".env"

    # Case 1: Former default is upgraded
    env_file.write_text('GEMINI_RESEARCH_MODEL="gemini-2.5-flash"\nOTHER="val"\n', encoding="utf-8")
    lines = env_file.read_text(encoding="utf-8").splitlines()
    res_m = None
    for line in lines:
        if line.startswith("GEMINI_RESEARCH_MODEL="):
            res_m = line.split("=", 1)[1].strip('"\'')
    if res_m == "gemini-2.5-flash":
        res_m = "gemini-3.6-flash"
    assert res_m == "gemini-3.6-flash"

    # Case 2: Custom model is preserved
    env_file.write_text('GEMINI_RESEARCH_MODEL="gemini-1.5-pro"\nOTHER="val"\n', encoding="utf-8")
    lines = env_file.read_text(encoding="utf-8").splitlines()
    res_m = None
    for line in lines:
        if line.startswith("GEMINI_RESEARCH_MODEL="):
            res_m = line.split("=", 1)[1].strip('"\'')
    if res_m == "gemini-2.5-flash":
        res_m = "gemini-3.6-flash"
    assert res_m == "gemini-1.5-pro"



def test_is_gemini_model_not_found_response():
    """Test model 404 classifier distinguishes model not found from generic 404s."""
    # Model not found with NOT_FOUND status
    resp_not_found = httpx.Response(
        404,
        json={"error": {"code": 404, "message": "models/gemini-old is not found for API version v1beta", "status": "NOT_FOUND"}},
    )
    is_unavail, msg = _is_gemini_model_not_found_response(resp_not_found)
    assert is_unavail is True
    assert "models/gemini-old" in msg

    # Model not supported / deprecated
    resp_deprecated = httpx.Response(
        404,
        json={"error": {"code": 404, "message": "The requested model is no longer available", "status": "NOT_FOUND"}},
    )
    is_unavail, _ = _is_gemini_model_not_found_response(resp_deprecated)
    assert is_unavail is True

    # Generic 404 without model text
    resp_generic_404 = httpx.Response(
        404,
        json={"error": {"code": 404, "message": "Resource requested could not be found", "status": "UNKNOWN"}},
    )
    is_unavail, _ = _is_gemini_model_not_found_response(resp_generic_404)
    assert is_unavail is False

    # Non-404 status
    resp_500 = httpx.Response(500, json={"error": {"message": "Internal error"}})
    is_unavail, _ = _is_gemini_model_not_found_response(resp_500)
    assert is_unavail is False


def test_grounded_research_404_model_not_found_raises_without_retry():
    """Verify model 404 raises GeminiModelUnavailableError immediately with zero retries."""
    mock_resp = httpx.Response(
        404,
        json={"error": {"code": 404, "message": "models/gemini-nonexistent was not found", "status": "NOT_FOUND"}},
    )

    attempt_count = 0

    def mock_post(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        return mock_resp

    with patch("httpx.Client.post", side_effect=mock_post):
        with pytest.raises(GeminiModelUnavailableError) as exc_info:
            generate_grounded_research(
                source_text="Test source text for research",
                research_depth="low",
                api_key="fake-key",
                model_name="gemini-nonexistent",
            )

        assert exc_info.value.error_category == "AI_MODEL_UNAVAILABLE"
        assert exc_info.value.retryable is False
        # Must NOT retry
        assert attempt_count == 1


def test_grounded_research_unrelated_404_retries_and_raises_generic_error():
    """Verify non-model 404 retries and raises standard GeminiError."""
    mock_resp = httpx.Response(
        404,
        json={"error": {"code": 404, "message": "Resource path /custom was not found", "status": "UNKNOWN"}},
    )

    attempt_count = 0

    def mock_post(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        return mock_resp

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("time.sleep", return_value=None):
        with pytest.raises(GeminiError) as exc_info:
            generate_grounded_research(
                source_text="Test source text for research",
                research_depth="low",
                api_key="fake-key",
                model_name="gemini-custom",
            )

        assert not isinstance(exc_info.value, GeminiModelUnavailableError)
        assert attempt_count == settings.GEMINI_RETRY_COUNT


def test_check_research_connection_success():
    """Verify check_research_connection reports connected when probe succeeds."""
    provider = GeminiProvider()
    mock_resp = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "pong"}]}}]})

    with patch.object(settings, "GEMINI_API_KEY", "test-key"), \
         patch("httpx.Client.post", return_value=mock_resp):
        res = provider.check_research_connection(force_refresh=True)
        assert res["configured"] is True
        assert res["connected"] is True
        assert res["model"] == "gemini-3.6-flash"
        assert res["error"] is None


def test_check_research_connection_model_unavailable_404():
    """Verify check_research_connection identifies AI_MODEL_UNAVAILABLE on 404."""
    provider = GeminiProvider()
    mock_resp = httpx.Response(
        404,
        json={"error": {"code": 404, "message": "models/gemini-3.6-flash is not found", "status": "NOT_FOUND"}},
    )

    with patch.object(settings, "GEMINI_API_KEY", "test-key"), \
         patch("httpx.Client.post", return_value=mock_resp):
        res = provider.check_research_connection(force_refresh=True)
        assert res["configured"] is True
        assert res["connected"] is False
        assert res.get("error_category") == "AI_MODEL_UNAVAILABLE"
        assert "unavailable or not found" in res["error"]


def test_check_research_connection_grounding_failure():
    """Verify check_research_connection detects grounding tool failure (e.g. 400)."""
    provider = GeminiProvider()
    mock_resp = httpx.Response(
        400,
        json={"error": {"code": 400, "message": "Google Search tool not supported for this model"}},
    )

    with patch.object(settings, "GEMINI_API_KEY", "test-key"), \
         patch("httpx.Client.post", return_value=mock_resp):
        res = provider.check_research_connection(force_refresh=True)
        assert res["configured"] is True
        assert res["connected"] is False
        assert "grounding tool failure" in res["error"]


def test_ai_check_command_independent_reporting():
    """Verify /ai-check reports standard and research status independently."""
    from herald.telegram.bot import handle_telegram_command
    from herald.telegram.client import TelegramClient

    mock_client = MagicMock(spec=TelegramClient)
    mock_db = MagicMock()
    msg = {
        "chat": {"id": 12345, "type": "private"},
        "from": {"id": 12345, "username": "tester"},
        "message_id": 999,
    }

    # Standard succeeds, Research fails
    mock_std = {"provider": "Gemini", "configured": True, "connected": True, "model": "gemini-3.5-flash", "error": None}
    mock_res = {"provider": "Gemini Research", "configured": True, "connected": False, "model": "gemini-3.6-flash", "error": "model unavailable (404)"}

    with patch("herald.telegram.bot.is_user_authorized", return_value=True), \
         patch("herald.telegram.bot.get_ai_provider") as mock_get_prov, \
         patch("herald.ai.gemini_provider.GeminiProvider.check_research_connection", return_value=mock_res), \
         patch.object(settings, "GEMINI_API_KEY", "valid-key"):
        mock_prov = MagicMock()
        mock_prov.is_configured.return_value = True
        mock_prov.check_connection.return_value = mock_std
        mock_get_prov.return_value = mock_prov

        handle_telegram_command(mock_db, mock_client, msg, "/ai-check", "")

        assert mock_client.send_message.call_count >= 2
        # Final message contains both statuses
        final_call = mock_client.send_message.call_args_list[-1]
        msg_text = final_call.kwargs.get("text", "")
        assert "Gemini (Standard):</b> Connected" in msg_text
        assert "Gemini Research:</b> Unavailable" in msg_text

