"""
Unit tests for Priority 2: AI Quota/Billing Classification, Circuit Breaker, and Anti-Storm Retry Control.
Tests:
- Classification of transient 429 (AI_RATE_LIMITED, retryable) vs billing/quota exhaustion (AI_QUOTA_EXHAUSTED, non-retryable).
- Immediate failover without inner 3x3 retry storm.
- Process-local circuit breaker activation, candidate skipping, and cooldown recovery.
- Gemini client immediate exit on depleted prepayment credits.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from herald.ai.circuit_breaker import (
    is_circuit_breaker_active,
    reset_circuit_breakers_for_tests,
    trip_circuit_breaker,
)
from herald.ai.errors import (
    AIQuotaExhaustedError,
    AIRateLimitedError,
)
from herald.ai.failover import execute_with_failover
from herald.ai.policy import classify_exception, decide_policy
from herald.db.models import PodcastJob
from herald.gemini.client import (
    GeminiBillingExhaustedError,
    _is_billing_exhausted_text,
    generate_podcast_script,
)


@pytest.fixture(autouse=True)
def reset_cb():
    reset_circuit_breakers_for_tests()
    yield
    reset_circuit_breakers_for_tests()


# ==============================================================================
# Error Classification Tests
# ==============================================================================

def test_classify_transient_rate_limit():
    exc = Exception("HTTP 429 Too Many Requests. Please slow down.")
    classified = classify_exception(exc, provider="gemini", model="gemini-2.5-flash")
    assert isinstance(classified, AIRateLimitedError)
    assert classified.category == "AI_RATE_LIMITED"
    assert classified.retryable is True


def test_classify_quota_depleted_prepayment():
    exc = Exception("Resource has been exhausted (e.g. check quota): 429 Your prepayment credits are depleted.")
    classified = classify_exception(exc, provider="gemini", model="gemini-2.5-flash")
    assert isinstance(classified, AIQuotaExhaustedError)
    assert classified.category == "AI_QUOTA_EXHAUSTED"
    assert classified.retryable is False


def test_classify_quota_exceeded_generic():
    exc = Exception("429 You have exceeded your current quota, please check your plan and billing details.")
    classified = classify_exception(exc, provider="gemini", model="gemini-2.5-flash")
    assert isinstance(classified, AIQuotaExhaustedError)
    assert classified.category == "AI_QUOTA_EXHAUSTED"
    assert classified.retryable is False


# ==============================================================================
# Policy Decision & Circuit Breaker Tripping Tests
# ==============================================================================

def test_policy_rate_limited_retries_same_provider():
    err = AIRateLimitedError("Rate limit hit", provider="gemini", model="gemini-2.5-flash")
    decision = decide_policy(err, attempt=1, max_attempts=3, has_next_candidate=True)
    assert decision.action == "RETRY_SAME_PROVIDER"
    # Circuit breaker should NOT be tripped for transient rate limit
    active, _ = is_circuit_breaker_active("gemini")
    assert active is False


def test_policy_quota_exhausted_fails_over_immediately():
    err = AIQuotaExhaustedError("Prepayment depleted", provider="gemini", model="gemini-2.5-flash")
    decision = decide_policy(err, attempt=1, max_attempts=3, has_next_candidate=True)
    assert decision.action == "FAILOVER_NEXT_PROVIDER"
    # Circuit breaker should be tripped immediately
    active, reason = is_circuit_breaker_active("gemini")
    assert active is True
    assert "Prepayment depleted" in reason


def test_policy_quota_exhausted_terminal_when_no_next_candidate():
    err = AIQuotaExhaustedError("Prepayment depleted", provider="gemini", model="gemini-2.5-flash")
    decision = decide_policy(err, attempt=1, max_attempts=3, has_next_candidate=False)
    assert decision.action == "FAIL_FINAL"


# ==============================================================================
# Circuit Breaker Cooldown & Recovery Tests
# ==============================================================================

def test_circuit_breaker_cooldown_expiration(monkeypatch):
    from herald.config import settings
    monkeypatch.setattr(settings, "AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 0.05)

    trip_circuit_breaker("gemini", "Prepayment credits depleted")
    active, _ = is_circuit_breaker_active("gemini")
    assert active is True

    time.sleep(0.06)
    active, _ = is_circuit_breaker_active("gemini")
    assert active is False


# ==============================================================================
# Gemini Client Immediate Exit on Depleted Credits Tests
# ==============================================================================

def test_gemini_client_halts_immediately_on_billing_depleted():
    """Verify gemini client does NOT loop 3 times when billing is depleted."""
    attempt_counter = 0

    def mock_post(*args, **kwargs):
        nonlocal attempt_counter
        attempt_counter += 1
        resp = MagicMock()
        resp.status_code = 429
        resp.text = '{"error": {"code": 429, "message": "Resource has been exhausted: Your prepayment credits are depleted."}}'
        resp.json.return_value = {"error": {"code": 429, "message": "Resource has been exhausted: Your prepayment credits are depleted."}}
        return resp

    with patch("herald.config.settings.GEMINI_API_KEY", "test-key"), \
         patch("httpx.Client.post", side_effect=mock_post):
        with pytest.raises(GeminiBillingExhaustedError):
            generate_podcast_script(source_text="Test source text")

    # Invariant: inner retry loop must exit on attempt 1 without executing retries 2 and 3
    assert attempt_counter == 1


# ==============================================================================
# Failover Candidate Skipping with Active Circuit Breaker Tests
# ==============================================================================

def test_failover_skips_provider_with_active_circuit_breaker():
    trip_circuit_breaker("gemini", "Prepayment credits depleted")

    chain = [
        {"provider": "gemini", "model": "gemini-2.5-flash"},
        {"provider": "cloudflare", "model": "cf-meta/llama"},
    ]
    job = PodcastJob(
        id="test-job-cb-123",
        transport="telegram",
        status="PENDING",
        ai_provider="gemini",
        ai_model="gemini-2.5-flash",
        ai_provider_chain_json=chain,
        ai_failover_index=0,
    )

    invoked_providers = []

    def mock_execute(provider_instance, attempt, source_text):
        invoked_providers.append(provider_instance.provider_name)
        return {"result": "success"}

    mock_cf = MagicMock()
    mock_cf.provider_name = "Cloudflare Workers AI"

    with patch("herald.ai.failover.is_provider_configured", return_value=True), \
         patch("herald.ai.failover.create_provider", return_value=mock_cf):
        res = execute_with_failover(
            job=job,
            operation="standard_script",
            execute_fn=mock_execute,
            source_text="Sample text",
        )

    assert res == {"result": "success"}
    # Gemini must have been skipped without being invoked
    assert invoked_providers == ["Cloudflare Workers AI"]
    # Failover cursor should point to Cloudflare
    assert job.ai_failover_index == 1
