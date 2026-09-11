"""
Unit tests for deterministic AI provider failover execution, sticky provider state,
capability skipping, and negative non-failover protections.
"""

from unittest.mock import MagicMock, patch
import pytest

from herald.ai.errors import (
    AIAuthFailedError,
    AIChainExhaustedError,
    AIModelUnavailableError,
    AIProviderTimeoutError,
    AIRateLimitedError,
)
from herald.ai.failover import execute_with_failover
from herald.db.models import PodcastJob


def create_test_job(chain: list[dict[str, str]], failover_index: int = 0) -> PodcastJob:
    job = PodcastJob(
        id="test-job-failover-123",
        transport="telegram",
        status="RECEIVED",
        ai_provider=chain[0]["provider"] if chain else "gemini",
        ai_model=chain[0]["model"] if chain else "gemini-3.5-flash",
        ai_provider_chain_json=chain,
        ai_failover_index=failover_index,
    )
    return job


def test_primary_success_does_not_call_secondary():
    """When Primary succeeds, Secondary is never invoked."""
    chain = [
        {"provider": "groq", "model": "groq/compound"},
        {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
    ]
    job = create_test_job(chain)
    calls = []

    def mock_exec(provider_instance, attempt):
        calls.append((provider_instance.provider_name, attempt))
        return "success-primary"

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        result = execute_with_failover(job, operation="script_generation", execute_fn=mock_exec)

    assert result == "success-primary"
    assert len(calls) == 1
    assert calls[0][0] == "Groq"
    assert job.ai_effective_provider == "groq"
    assert job.ai_failover_index == 0


def test_primary_failure_moves_to_secondary_and_becomes_sticky():
    """Primary 429 exhaust moves to Secondary; Secondary becomes sticky active provider."""
    chain = [
        {"provider": "groq", "model": "groq/compound"},
        {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
    ]
    job = create_test_job(chain)
    calls = []

    def mock_exec(provider_instance, attempt):
        calls.append((provider_instance.provider_name, attempt))
        if provider_instance.provider_name == "Groq":
            raise AIRateLimitedError("Groq 429 quota exhausted", retry_after_seconds=0.01)
        return "success-cloudflare"

    with patch("herald.ai.failover.is_provider_configured", return_value=True), \
         patch("time.sleep", return_value=None):
        result = execute_with_failover(
            job,
            operation="script_generation",
            execute_fn=mock_exec,
            max_same_provider_attempts=2,
        )

    assert result == "success-cloudflare"
    # Groq attempted 2 times (same-provider retry budget), then failed over to Cloudflare
    assert calls == [
        ("Groq", 1),
        ("Groq", 2),
        ("Cloudflare Workers AI", 1),
    ]
    # Invariant: Sticky provider is now Cloudflare, failover index is 1
    assert job.ai_effective_provider == "cloudflare"
    assert job.ai_effective_model == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    assert job.ai_failover_index == 1

    # Second subsequent operation on same job MUST NOT return to Groq!
    subsequent_calls = []

    def mock_second_exec(provider_instance, attempt):
        subsequent_calls.append(provider_instance.provider_name)
        return "success-subsequent"

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        res2 = execute_with_failover(
            job,
            operation="verification",
            execute_fn=mock_second_exec,
        )

    assert res2 == "success-subsequent"
    assert subsequent_calls == ["Cloudflare Workers AI"]  # Began directly at Cloudflare!


def test_secondary_failure_moves_to_tertiary():
    """Primary and Secondary fail, Tertiary succeeds."""
    chain = [
        {"provider": "groq", "model": "groq/compound"},
        {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
        {"provider": "openai", "model": "gpt-4o"},
    ]
    job = create_test_job(chain)
    calls = []

    def mock_exec(provider_instance, attempt):
        calls.append(provider_instance.provider_name)
        if provider_instance.provider_name == "Groq":
            raise AIAuthFailedError("Groq invalid key")  # Immediate failover
        if provider_instance.provider_name == "Cloudflare Workers AI":
            raise AIModelUnavailableError("Cloudflare model retired")  # Immediate failover
        return "success-openai"

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        result = execute_with_failover(job, operation="script_generation", execute_fn=mock_exec)

    assert result == "success-openai"
    assert calls == ["Groq", "Cloudflare Workers AI", "OpenAI"]
    assert job.ai_effective_provider == "openai"
    assert job.ai_failover_index == 2


def test_chain_exhaustion_returns_combined_safe_failure():
    """All candidates fail -> AIChainExhaustedError with safe summary."""
    chain = [
        {"provider": "groq", "model": "groq/compound"},
        {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
    ]
    job = create_test_job(chain)

    def mock_exec(provider_instance, attempt):
        if provider_instance.provider_name == "Groq":
            raise AIAuthFailedError("Groq 401 unauthorized")
        raise AIProviderTimeoutError("Cloudflare 504 timeout")

    with patch("herald.ai.failover.is_provider_configured", return_value=True), \
         patch("time.sleep", return_value=None):
        with pytest.raises(AIChainExhaustedError) as exc_info:
            execute_with_failover(
                job,
                operation="script_generation",
                execute_fn=mock_exec,
                max_same_provider_attempts=1,
            )

    err_msg = str(exc_info.value)
    assert "AI providers exhausted" in err_msg
    assert "groq" in err_msg
    assert "cloudflare" in err_msg


def test_failover_never_uses_provider_outside_snapshot():
    """A job configured with Groq and Cloudflare never invokes Gemini merely because Gemini credentials exist."""
    chain = [
        {"provider": "groq", "model": "groq/compound"},
        {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
    ]
    job = create_test_job(chain)
    invoked_providers = []

    def mock_exec(provider_instance, attempt):
        invoked_providers.append(provider_instance.provider_name)
        raise AIAuthFailedError("Failure")

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        with pytest.raises(AIChainExhaustedError):
            execute_with_failover(
                job,
                operation="script_generation",
                execute_fn=mock_exec,
                max_same_provider_attempts=1,
            )

    assert "Gemini" not in invoked_providers
    assert invoked_providers == ["Groq", "Cloudflare Workers AI"]


def test_capability_aware_skipping():
    """Candidate lacking required capability is skipped without executing or downgrading."""
    chain = [
        {"provider": "groq", "model": "groq/compound"},  # lacks research_grounding
        {"provider": "gemini", "model": "gemini-3.6-flash"},  # has research_grounding
    ]
    job = create_test_job(chain)
    invoked = []

    def mock_exec(provider_instance, attempt):
        invoked.append(provider_instance.provider_name)
        return "grounded-dossier"

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        res = execute_with_failover(
            job,
            operation="research_grounding",
            execute_fn=mock_exec,
            required_capability="research_grounding",
        )

    assert res == "grounded-dossier"
    # Groq was skipped via capability check; only Gemini executed!
    assert invoked == ["Gemini"]
    assert job.ai_effective_provider == "gemini"


def test_recovery_resumes_from_failover_cursor():
    """If server restarts after failover advanced to index 1, execution resumes at candidate 1."""
    chain = [
        {"provider": "groq", "model": "groq/compound"},
        {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
    ]
    # Simulate job recovered from DB with ai_failover_index = 1
    job = create_test_job(chain, failover_index=1)
    invoked = []

    def mock_exec(provider_instance, attempt):
        invoked.append(provider_instance.provider_name)
        return "success-recovery"

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        res = execute_with_failover(job, operation="script_generation", execute_fn=mock_exec)

    assert res == "success-recovery"
    # Groq was NOT called again; started directly at Cloudflare!
    assert invoked == ["Cloudflare Workers AI"]


def test_negative_failover_programmer_error_does_not_failover():
    """
    User Correction 26: Programmer bugs, TypeError, AttributeError, or malformed
    internal state must NOT trigger failover to another provider.
    """
    chain = [
        {"provider": "groq", "model": "groq/compound"},
        {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
    ]
    job = create_test_job(chain)
    invoked = []

    def mock_bug_exec(provider_instance, attempt):
        invoked.append(provider_instance.provider_name)
        # Simulate application programmer bug
        raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        with pytest.raises(Exception) as exc_info:
            execute_with_failover(job, operation="script_generation", execute_fn=mock_bug_exec)

    # Prove Secondary was NEVER invoked for a programmer bug!
    assert "Cloudflare Workers AI" not in invoked
    assert invoked == ["Groq"]
    assert job.ai_failover_index == 0


def test_literal_mode_has_zero_provider_calls():
    """Literal mode short-circuits completely, zero AI calls, no failover."""
    chain = [{"provider": "literal", "model": "none"}]
    job = create_test_job(chain)
    job.request_mode = "literal"
    invoked = []

    def mock_exec(provider_instance, attempt):
        invoked.append(provider_instance.provider_name)
        return "literal-result"

    result = execute_with_failover(job, operation="script_generation", execute_fn=mock_exec)
    assert result == "literal-result"
    assert invoked == ["None (Literal)"]
