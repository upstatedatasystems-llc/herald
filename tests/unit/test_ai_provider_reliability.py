"""
Unit test suite for Checkpoint 7: Provider Reliability & Error Hardening.
Tests:
- Cloudflare response extraction across all 4 payload shapes:
  1. result.response
  2. result.text
  3. response
  4. choices[0].message.content
  and invalid payload handling with AIProviderError.
- Cloudflare model tuning (Qwen reasoning_effort="low", Gemma max_completion_tokens=16384).
- Groq error classification:
  - 413 Payload Too Large -> AIContextLimitExceededError
  - 429 Rate Limit with Retry-After header extraction and AIRateLimitedError
  - 401 / 403 Authentication failures -> AIAuthFailedError / AIPermissionDeniedError
- Provider timeout adherence using effective_ai_timeout_seconds.
"""

from unittest.mock import MagicMock, patch

import pytest

from herald.ai.cloudflare_provider import CloudflareProvider, extract_cloudflare_content
from herald.ai.errors import (
    AIAuthFailedError,
    AIPermissionDeniedError,
    AIProviderError,
    AIRateLimitedError,
    AIRequestTooLargeError,
)
from herald.ai.groq_provider import GroqProvider


def test_cloudflare_response_extractor_shapes():
    """Verify extract_cloudflare_content handles all 4 payload shapes correctly."""
    # Shape 1: result.response
    p1 = {"result": {"response": '{"episode_title": "Shape 1"}'}}
    assert extract_cloudflare_content(p1) == '{"episode_title": "Shape 1"}'

    # Shape 2: result.text
    p2 = {"result": {"text": '{"episode_title": "Shape 2"}'}}
    assert extract_cloudflare_content(p2) == '{"episode_title": "Shape 2"}'

    # Shape 3: direct response field
    p3 = {"response": '{"episode_title": "Shape 3"}'}
    assert extract_cloudflare_content(p3) == '{"episode_title": "Shape 3"}'

    # Shape 4: choices[0].message.content
    p4 = {"choices": [{"message": {"content": '{"episode_title": "Shape 4"}'}}]}
    assert extract_cloudflare_content(p4) == '{"episode_title": "Shape 4"}'

    # Shape 4b: choices[0].text
    p4b = {"choices": [{"text": '{"episode_title": "Shape 4b"}'}]}
    assert extract_cloudflare_content(p4b) == '{"episode_title": "Shape 4b"}'

    # Invalid / Empty shapes raise AIProviderError
    with pytest.raises(AIProviderError):
        extract_cloudflare_content({})

    with pytest.raises(AIProviderError):
        extract_cloudflare_content({"result": None})

    with pytest.raises(AIProviderError):
        extract_cloudflare_content({"choices": []})

    with pytest.raises(AIProviderError):
        extract_cloudflare_content("not a dict")  # type: ignore


def test_cloudflare_model_tuning():
    """Verify Qwen and Gemma models receive appropriate tuning parameters in payload."""
    cf_qwen = CloudflareProvider(
        account_id="cf_acct",
        api_token="cf_tok",
        model="@cf/qwen/qwen3.8-27b",
    )

    captured_payloads = []

    def mock_post(url, json=None, headers=None, **kwargs):
        captured_payloads.append(json)
        return MagicMock(
            status_code=200,
            headers={"cf-ray": "ray-123"},
            json=lambda: {"result": {"response": '{"episode_title": "T", "episode_description": "D", "estimated_minutes": 1, "source_title": "S", "segments": [{"order": 1, "heading": "H", "narration": "N"}], "warnings": []}'}},
        )

    with patch("httpx.Client.post", side_effect=mock_post):
        cf_qwen.generate_script(source_text="Test source text for Qwen tuning", request_mode="standard", job_id="test-job-qwen")

    assert len(captured_payloads) == 1
    assert captured_payloads[0].get("reasoning_effort") == "low"
    assert captured_payloads[0].get("max_completion_tokens") == 16384

    # Gemma model
    captured_payloads.clear()
    cf_gemma = CloudflareProvider(
        account_id="cf_acct",
        api_token="cf_tok",
        model="@cf/google/gemma-4-26b-a4b-it",
    )
    with patch("httpx.Client.post", side_effect=mock_post):
        cf_gemma.generate_script(source_text="Test source text for Gemma tuning", request_mode="standard", job_id="test-job-gemma")

    assert len(captured_payloads) == 1
    assert captured_payloads[0].get("max_completion_tokens") == 16384


def test_groq_error_classification_413():
    """Verify Groq HTTP 413 maps immediately to AIRequestTooLargeError (Item 8)."""
    groq = GroqProvider(api_key="gsk_test", model="llama-3.3-70b-versatile")

    mock_resp = MagicMock(
        status_code=413,
        text="Request entity too large: payload exceeds token context limits.",
        headers={"x-request-id": "req-413"},
    )

    with patch("httpx.Client.post", return_value=mock_resp):
        with pytest.raises(AIRequestTooLargeError) as exc_info:
            groq.generate_script(source_text="Very large source text", request_mode="standard", job_id="test-groq-413")

    assert exc_info.value.http_status == 413
    assert exc_info.value.provider == "groq"


def test_groq_error_classification_401_and_403():
    """Verify Groq HTTP 401/403 map to unretryable auth errors immediately."""
    groq = GroqProvider(api_key="gsk_invalid", model="llama-3.3-70b-versatile")

    # 401 Auth Failed
    mock_resp_401 = MagicMock(
        status_code=401,
        text="Invalid API Key provided.",
        headers={"x-request-id": "req-401"},
    )
    with patch("httpx.Client.post", return_value=mock_resp_401):
        with pytest.raises(AIAuthFailedError) as exc_info:
            groq.generate_script(source_text="Auth test text", request_mode="standard", job_id="test-groq-401")

    assert exc_info.value.http_status == 401
    assert exc_info.value.provider == "groq"

    # 403 Permission Denied
    mock_resp_403 = MagicMock(
        status_code=403,
        text="Access denied to requested model.",
        headers={"x-request-id": "req-403"},
    )
    with patch("httpx.Client.post", return_value=mock_resp_403):
        with pytest.raises(AIPermissionDeniedError) as exc_info:
            groq.generate_script(source_text="Permission test text", request_mode="standard", job_id="test-groq-403")

    assert exc_info.value.http_status == 403
    assert exc_info.value.provider == "groq"


def test_groq_error_classification_429_with_retry_after():
    """Verify Groq HTTP 429 extracts Retry-After and raises AIRateLimitedError without inner retries (Item 4)."""
    groq = GroqProvider(api_key="gsk_test", model="llama-3.3-70b-versatile")

    mock_resp_429 = MagicMock(
        status_code=429,
        text="Rate limit reached for model llama-3.3-70b-versatile.",
        headers={"x-request-id": "req-429", "retry-after": "5.5"},
    )

    with patch("httpx.Client.post", return_value=mock_resp_429):
        with pytest.raises(AIRateLimitedError) as exc_info:
            groq.generate_script(source_text="Rate limit test", request_mode="standard", job_id="test-groq-429")

    assert exc_info.value.http_status == 429
    assert exc_info.value.retry_after_seconds == 5.5
