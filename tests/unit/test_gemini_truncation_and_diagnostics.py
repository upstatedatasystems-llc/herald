"""
Unit test matrix for Gemini script output truncation, adaptive retries,
thinking level configuration, finishReason/thought token telemetry,
and malformed vs truncated JSON classification.
"""

import json
from unittest.mock import patch

import httpx
import pytest

from herald.config import settings
from herald.gemini.client import (
    GeminiOutputTruncatedError,
    _extract_tokens,
    generate_podcast_script,
    supports_thinking_budget,
)
from herald.services.redaction import sanitize_error


def _make_gemini_response(
    script_dict: dict | None = None,
    raw_text: str | None = None,
    finish_reason: str = "STOP",
    prompt_tokens: int = 2000,
    candidates_tokens: int = 1000,
    total_tokens: int = 3000,
    thought_tokens: int | None = 500,
    status_code: int = 200,
) -> httpx.Response:
    if raw_text is None:
        raw_text = json.dumps(
            script_dict
            or {
                "episode_title": "Test Episode",
                "episode_description": "Test Description",
                "estimated_minutes": 5,
                "source_title": "Test Source",
                "segments": [
                    {
                        "order": 1,
                        "heading": "Intro",
                        "narration": "This is test narration.",
                    }
                ],
                "warnings": [],
            }
        )

    body = {
        "candidates": [
            {
                "content": {"parts": [{"text": raw_text}], "role": "model"},
                "finishReason": finish_reason,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidates_tokens,
            "totalTokenCount": total_tokens,
        },
    }
    if thought_tokens is not None:
        body["usageMetadata"]["thoughtsTokenCount"] = thought_tokens

    return httpx.Response(status_code, json=body)


def test_extract_tokens_includes_thought_tokens():
    """Test _extract_tokens safely extracts prompt, candidates, total, and thought tokens."""
    data = {
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 200,
            "totalTokenCount": 350,
            "thoughtsTokenCount": 50,
        }
    }
    p, c, t, th = _extract_tokens(data)
    assert p == 100
    assert c == 200
    assert t == 350
    assert th == 50

    # Test nested candidatesTokensDetails fallback
    data_details = {
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 200,
            "totalTokenCount": 300,
            "candidatesTokensDetails": [{"modality": "THOUGHT", "tokenCount": 75}],
        }
    }
    p, c, t, th = _extract_tokens(data_details)
    assert th == 75

    # None handling
    assert _extract_tokens(None) == (None, None, None, None)


def test_supports_thinking_budget():
    """Test model capability helper for thinking config support."""
    assert supports_thinking_budget("gemini-3.5-flash") is True
    assert supports_thinking_budget("gemini-2.5-flash") is True
    assert supports_thinking_budget("gemini-2.5-pro") is True
    assert supports_thinking_budget("gemini-2.0-flash-thinking-exp") is True
    assert supports_thinking_budget("gemini-1.5-flash") is False
    assert supports_thinking_budget("gemini-1.5-pro") is False
    assert supports_thinking_budget("gemini-1.0-pro") is False


def test_case_a_normal_valid_gemini_json(monkeypatch):
    """
    Test Matrix A: Normal valid Gemini JSON with finishReason=STOP succeeds on first attempt.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "GEMINI_SCRIPT_MAX_OUTPUT_TOKENS", 16384)

    mock_resp = _make_gemini_response(finish_reason="STOP")

    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        return mock_resp

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("herald.gemini.client.record_ai_interaction") as mock_record:
        res = generate_podcast_script(source_text="Test source text", job_id="test-job-1")

    assert res.episode_title == "Test Episode"
    assert len(posted_payloads) == 1
    # Verify thinking config is present for gemini-3.5-flash
    gen_cfg = posted_payloads[0]["generationConfig"]
    assert gen_cfg["maxOutputTokens"] == 16384
    assert gen_cfg["thinkingConfig"] == {"thinkingBudget": 1024}

    # Verify telemetry recorded
    assert mock_record.called
    kwargs = mock_record.call_args[1]
    assert kwargs["success"] is True
    assert kwargs["metadata"]["finish_reason"] == "STOP"
    assert kwargs["metadata"]["thought_tokens"] == 500
    assert kwargs["metadata"]["requested_max_output_tokens"] == 16384


def test_case_b_normally_completed_malformed_json_triggers_repair_prompt(monkeypatch):
    """
    Test Matrix B: finishReason=STOP with malformed JSON triggers structured repair retry with error note in prompt.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "GEMINI_RETRY_COUNT", 2)

    # Attempt 1: malformed JSON but finishReason=STOP
    resp1 = _make_gemini_response(raw_text='{"episode_title": "Broken"', finish_reason="STOP")
    # Attempt 2: valid JSON
    resp2 = _make_gemini_response(finish_reason="STOP")

    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        if len(posted_payloads) == 1:
            return resp1
        return resp2

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("time.sleep"):
        res = generate_podcast_script(source_text="Test source text", job_id="test-job-2")

    assert res.episode_title == "Test Episode"
    assert len(posted_payloads) == 2
    # Verify attempt 2 prompt has the structured repair note
    attempt2_prompt = posted_payloads[1]["contents"][0]["parts"][0]["text"]
    assert "Previous attempt failed validation:" in attempt2_prompt


def test_case_c_and_d_truncated_json_adaptive_retry(monkeypatch):
    """
    Test Matrix C and D: finishReason=MAX_TOKENS with truncated JSON is classified as OUTPUT_TRUNCATED,
    does not send malformed prompt repair note, adaptively increases maxOutputTokens (16384 to 32768),
    and succeeds on second attempt.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "GEMINI_SCRIPT_MAX_OUTPUT_TOKENS", 16384)
    monkeypatch.setattr(settings, "GEMINI_RETRY_COUNT", 3)

    # Attempt 1: truncated JSON with finishReason=MAX_TOKENS
    resp1 = _make_gemini_response(
        raw_text='{"episode_title": "Truncated...',
        finish_reason="MAX_TOKENS",
        thought_tokens=2000,
    )
    # Attempt 2: valid JSON with finishReason=STOP
    resp2 = _make_gemini_response(finish_reason="STOP", thought_tokens=1024)

    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        if len(posted_payloads) == 1:
            return resp1
        return resp2

    recorded_calls = []

    def mock_record(*args, **kwargs):
        recorded_calls.append(kwargs)

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("herald.gemini.client.record_ai_interaction", side_effect=mock_record), \
         patch("time.sleep"):
        res = generate_podcast_script(source_text="Long article source", job_id="test-job-3")

    assert res.episode_title == "Test Episode"
    assert len(posted_payloads) == 2

    # Attempt 1 checked 16384
    assert posted_payloads[0]["generationConfig"]["maxOutputTokens"] == 16384
    # Attempt 2 increased budget to 32768
    assert posted_payloads[1]["generationConfig"]["maxOutputTokens"] == 32768

    # Attempt 2 prompt must NOT have malformed JSON repair note
    attempt2_prompt = posted_payloads[1]["contents"][0]["parts"][0]["text"]
    assert "Previous attempt failed validation:" not in attempt2_prompt

    # Verify attempt 1 was recorded as OUTPUT_TRUNCATED error category
    rec1 = recorded_calls[0]
    assert rec1["success"] is False
    assert rec1["error_category"] == "OUTPUT_TRUNCATED"
    assert rec1["metadata"]["finish_reason"] == "MAX_TOKENS"
    assert rec1["metadata"]["requested_max_output_tokens"] == 16384
    assert rec1["metadata"]["thought_tokens"] == 2000


def test_case_e_repeated_truncation_fails_as_output_truncated(monkeypatch):
    """
    Test Matrix E: Repeated truncation across all retry attempts results in a controlled
    GeminiOutputTruncatedError rather than generic schema error.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "GEMINI_SCRIPT_MAX_OUTPUT_TOKENS", 16384)
    monkeypatch.setattr(settings, "GEMINI_RETRY_COUNT", 2)

    resp_trunc = _make_gemini_response(
        raw_text='{"episode_title": "Never finishes...',
        finish_reason="MAX_TOKENS",
    )

    with patch("httpx.Client.post", return_value=resp_trunc), \
         patch("time.sleep"):
        with pytest.raises(GeminiOutputTruncatedError) as exc_info:
            generate_podcast_script(source_text="Massive article", job_id="test-job-4")

    assert "finishReason=MAX_TOKENS" in str(exc_info.value)
    cat, msg = sanitize_error(exc_info.value)
    assert cat == "OUTPUT_TRUNCATED"


def test_gemini_script_thinking_omitted_for_unsupported_models(monkeypatch):
    """
    Test Matrix G: When configured with a model that does not support thinkingConfig (e.g. gemini-1.5-flash),
    thinkingConfig is cleanly omitted from payload.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-1.5-flash")

    mock_resp = _make_gemini_response(finish_reason="STOP")

    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        return mock_resp

    with patch("httpx.Client.post", side_effect=mock_post):
        generate_podcast_script(source_text="Test source text", job_id="test-job-5")

    assert len(posted_payloads) == 1
    gen_cfg = posted_payloads[0]["generationConfig"]
    assert "thinkingConfig" not in gen_cfg
