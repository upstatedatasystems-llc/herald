"""
Unit tests for deterministic AI provider failover execution, sticky provider state,
capability skipping, and negative non-failover protections.
"""

from unittest.mock import patch

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

    def mock_exec(provider_instance, attempt, src=None):
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

    def mock_exec(provider_instance, attempt, src=None):
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

    def mock_second_exec(provider_instance, attempt, src=None):
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

    def mock_exec(provider_instance, attempt, src=None):
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

    def mock_exec(provider_instance, attempt, src=None):
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

    def mock_exec(provider_instance, attempt, src=None):
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

    def mock_exec(provider_instance, attempt, src=None):
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

    def mock_exec(provider_instance, attempt, src=None):
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

    def mock_bug_exec(provider_instance, attempt, src=None):
        invoked.append(provider_instance.provider_name)
        # Simulate application programmer bug
        raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        with pytest.raises(Exception):
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

    def mock_exec(provider_instance, attempt, src=None):
        invoked.append(provider_instance.provider_name)
        return "literal-result"

    result = execute_with_failover(job, operation="script_generation", execute_fn=mock_exec)
    assert result == "literal-result"
    assert invoked == ["None (Literal)"]


def test_failover_policy_strict_allowlist():
    """Verify unclassified errors and ValueError produce FAIL_FINAL."""
    from herald.ai.policy import ActionType, decide_failover_action

    act = decide_failover_action(ValueError("Invalid argument in prompt"), attempt=1, max_attempts=1, has_next_candidate=True)
    assert act.action == ActionType.FAIL_FINAL

    act = decide_failover_action(TypeError("Cannot concatenate str to NoneType"), attempt=1, max_attempts=1, has_next_candidate=True)
    assert act.action == ActionType.FAIL_FINAL

    act = decide_failover_action(RuntimeError("Unexpected OS condition"), attempt=1, max_attempts=1, has_next_candidate=True)
    assert act.action == ActionType.FAIL_FINAL

    act = decide_failover_action(AIAuthFailedError("Bad token", provider="groq"), attempt=1, max_attempts=1, has_next_candidate=True)
    assert act.action == ActionType.FAILOVER_NEXT_PROVIDER


def test_fail_final_never_advances_cursor(db_session):
    """Verify FAIL_FINAL terminates immediately without advancing job.ai_failover_index."""
    from unittest.mock import MagicMock

    job = PodcastJob(
        id="job-fail-final",
        transport="telegram",
        source_hash="h1",
        source_text="Sample text",
        status="RECEIVED",
        ai_provider="groq",
        ai_model="llama-3.3-70b-versatile",
        ai_provider_chain_json=[
            {"provider": "groq", "model": "llama-3.3-70b-versatile"},
            {"provider": "openai", "model": "gpt-4o"},
        ],
        ai_failover_index=0,
    )
    db_session.add(job)
    db_session.commit()

    def _buggy_fn(p, att):
        raise ValueError("Invalid internal formatting")

    with patch("herald.ai.failover.is_provider_configured", return_value=True), \
         patch("herald.ai.failover.create_provider", return_value=MagicMock()):
        with pytest.raises(ValueError):
            execute_with_failover(
                job=job,
                operation="script",
                execute_fn=_buggy_fn,
                db=db_session,
                max_same_provider_attempts=1,
            )

    assert job.ai_failover_index == 0


def test_unknown_provider_id_never_becomes_literal():
    """Verify unknown provider ID raises ValueError and is never silently transformed to Literal."""
    from herald.ai.registry import create_provider, is_provider_registered

    assert not is_provider_registered("mystery_ai")
    with pytest.raises(ValueError, match="Unknown AI provider"):
        create_provider("mystery_ai")


def test_resolved_job_settings_to_snapshot():
    """Verify snapshot contains effective resolved values, research identity, and complete chain."""
    from herald.ai.resolution import AIProviderCandidate, ResolvedJobSettings

    resolved = ResolvedJobSettings(
        mode="research",
        research_depth="deep",
        voice="am_adam",
        speed=1.1,
        custom_title="Custom Ep",
        chunk_chars=600,
        verify=True,
        ai_candidates=[
            AIProviderCandidate(provider_id="gemini", model_id="gemini-3.5-flash"),
            AIProviderCandidate(provider_id="groq", model_id="llama-3.3-70b-versatile"),
        ],
        research_provider="gemini",
        research_model="gemini-3.6-flash",
    )
    snap = resolved.to_snapshot()
    assert snap["mode"] == "research"
    assert snap["voice"] == "am_adam"
    assert snap["speed"] == 1.1
    assert snap["ai_provider"] == "gemini"
    assert snap["ai_model"] == "gemini-3.5-flash"
    assert snap["research_provider"] == "gemini"
    assert snap["research_model"] == "gemini-3.6-flash"
    assert len(snap["ai_provider_chain"]) == 2


def test_explicit_primary_override_preserves_fallbacks():
    """Verify explicit request-level primary override preserves user's secondary and tertiary fallbacks."""
    from herald.ai.resolution import resolve_job_settings

    usr_prefs = {
        "ai_provider_chain_json": ["groq", "cloudflare", "openai"],
    }
    req = {"ai_provider": "gemini"}
    resolved = resolve_job_settings(request_params=req, user_prefs=usr_prefs)
    chain_provs = [c.provider_id for c in resolved.ai_candidates]
    assert chain_provs == ["gemini", "groq", "cloudflare"]


def test_fail_final_never_advances_cursor_execution():
    """Verify FAIL_FINAL immediately raises without advancing cursor to next candidate."""
    from herald.ai.errors import AIProviderError

    chain = [
        {"provider": "groq", "model": "groq/compound"},
        {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
        {"provider": "openai", "model": "gpt-4o"},
    ]
    job = create_test_job(chain, failover_index=0)
    invoked = []

    def mock_exec(prov, attempt, src=None):
        invoked.append(prov.provider_name)
        raise AIProviderError("Terminal unrecoverable error", category="unclassified")

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        with pytest.raises(AIProviderError, match="Terminal unrecoverable error"):
            execute_with_failover(
                job,
                operation="script_generation",
                execute_fn=mock_exec,
            )

    assert invoked == ["Groq"]
    assert job.ai_failover_index == 0


def test_configured_secondary_keeps_ai_mode_available_when_primary_unconfigured():
    """When Primary is unconfigured but Secondary has valid credentials, AI mode is available."""
    from herald.ai.resolution import resolve_job_settings

    with patch("herald.config.settings.AI_PROVIDER", "gemini"), \
         patch("herald.config.settings.AI_SECONDARY_PROVIDER", "groq"), \
         patch("herald.config.settings.AI_TERTIARY_PROVIDER", None), \
         patch("herald.config.settings.GEMINI_API_KEY", ""), \
         patch("herald.config.settings.GROQ_API_KEY", "gsk_valid_key"), \
         patch("herald.ai.registry.is_provider_configured", side_effect=lambda p: p == "groq"):
        resolved = resolve_job_settings(request_params={}, user_prefs={})

    assert resolved.mode == "standard"
    assert len(resolved.ai_candidates) == 2


def test_do_not_silently_repair_invalid_server_chains():
    """Verify get_server_default_chain raises ValueError on invalid configuration."""
    from unittest.mock import MagicMock

    from herald.ai.resolution import get_server_default_chain

    mock_cfg = MagicMock()
    mock_cfg.AI_PROVIDER = "groq"
    mock_cfg.AI_SECONDARY_PROVIDER = "groq"  # Duplicate -> invalid chain
    mock_cfg.AI_TERTIARY_PROVIDER = None

    with pytest.raises(ValueError) as exc:
        get_server_default_chain(mock_cfg)

    assert "Invalid server default AI provider chain" in str(exc.value)

