from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.ai.adaptation import AdaptationBudget, AdaptationUsage, adapt_source_text
from herald.ai.catalog import (
    generate_model_token,
    get_models_for_provider,
    resolve_model_token,
)
from herald.ai.cloudflare_provider import CloudflareProvider, extract_cloudflare_content
from herald.ai.errors import (
    AIAuthFailedError,
    AIClientTimeoutError,
    AIContextExceededError,
    AIPermissionDeniedError,
    AIProviderError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
    AIRateLimitedError,
    AIRequestTooLargeError,
)
from herald.ai.failover import execute_with_failover
from herald.ai.groq_provider import GroqProvider
from herald.ai.openai_provider import OpenAIProvider
from herald.ai.policy import ActionType, decide_failover_action
from herald.ai.registry import (
    create_provider,
    is_provider_registered,
)
from herald.ai.resolution import (
    AIProviderCandidate,
    ResolvedJobSettings,
    resolve_job_settings,
)
from herald.db.models import Base, PodcastJob, TelegramUser
from herald.extraction.url_extractor import extract_article_from_url
from herald.telegram.auth import (
    set_user_ai_provider_chain,
)


def create_test_job(chain: list[dict[str, str]], failover_index: int = 0) -> PodcastJob:
    return PodcastJob(
        id="test-job-regress-123",
        transport="telegram",
        status="RECEIVED",
        ai_provider=chain[0]["provider"] if chain else "gemini",
        ai_model=chain[0]["model"] if chain else "gemini-3.5-flash",
        ai_provider_chain_json=chain,
        ai_failover_index=failover_index,
    )


def test_cloudflare_all_4_response_shapes():
    """Verify parser extracts content from all 4 Cloudflare Workers AI response schemas."""
    s1 = {"result": {"response": '{"episode_title": "Title 1"}'}}
    assert extract_cloudflare_content(s1) == '{"episode_title": "Title 1"}'

    s2 = {"result": {"choices": [{"message": {"content": '{"episode_title": "Title 2"}'}}]}}
    assert extract_cloudflare_content(s2) == '{"episode_title": "Title 2"}'

    s3 = {"response": '{"episode_title": "Title 3"}'}
    assert extract_cloudflare_content(s3) == '{"episode_title": "Title 3"}'

    s4 = {"choices": [{"message": {"content": '{"episode_title": "Title 4"}'}}]}
    assert extract_cloudflare_content(s4) == '{"episode_title": "Title 4"}'


def test_cloudflare_error_classification():
    """Verify Cloudflare timeout and HTTP error mapping."""
    prov = CloudflareProvider(account_id="acc", api_token="tok")
    req = httpx.Request("POST", "https://api.cloudflare.com")

    with pytest.raises(AIClientTimeoutError):
        prov._classify_transport_error(httpx.TimeoutException("Read timed out", request=req))

    resp_408 = httpx.Response(408, request=req, text="Request Timeout")
    with pytest.raises(AIProviderTimeoutError):
        prov._classify_http_error(resp_408)

    resp_401 = httpx.Response(401, request=req, text="Unauthorized token")
    with pytest.raises(AIAuthFailedError):
        prov._classify_http_error(resp_401)

    resp_403 = httpx.Response(403, request=req, text="Forbidden access")
    with pytest.raises(AIPermissionDeniedError):
        prov._classify_http_error(resp_403)

    resp_500 = httpx.Response(500, request=req, text="Internal server error")
    with pytest.raises(AIProviderUnavailableError):
        prov._classify_http_error(resp_500)


def test_cloudflare_qwen_and_gemma_authoritative_tuning():
    """Verify verified catalog Qwen and Gemma request payloads contain authoritative tuning, but unverified models do not."""
    # 1. Verified catalog models get authoritative tuning
    prov_qwen_verified = CloudflareProvider(
        account_id="acc",
        api_token="tok",
        model="@cf/qwen/qwen3.8-27b",
    )
    payload_qwen = prov_qwen_verified._build_request_payload(
        system_prompt="sys",
        user_prompt="usr",
        max_output_tokens=16384,
    )
    assert payload_qwen["max_tokens"] == 16384
    assert payload_qwen["reasoning_effort"] == "low"
    assert payload_qwen["max_completion_tokens"] == 16384

    prov_gemma_verified = CloudflareProvider(
        account_id="acc",
        api_token="tok",
        model="@cf/google/gemma-4-26b-a4b-it",
    )
    payload_gemma = prov_gemma_verified._build_request_payload(
        system_prompt="sys",
        user_prompt="usr",
        max_output_tokens=16384,
    )
    assert payload_gemma["max_tokens"] == 16384
    assert payload_gemma["reasoning_effort"] == "low"
    assert payload_gemma["max_completion_tokens"] == 16384

    # 2. Unverified arbitrary model with 'qwen' or 'gemma' in name must NOT get special tuning
    prov_unverified = CloudflareProvider(
        account_id="acc",
        api_token="tok",
        model="@cf/qwen/custom-unverified-qwen-model",
    )
    payload_unverified = prov_unverified._build_request_payload(
        system_prompt="sys",
        user_prompt="usr",
    )
    assert "reasoning_effort" not in payload_unverified
    assert "max_completion_tokens" not in payload_unverified


def test_groq_413_vs_context_exceeded():
    """Verify HTTP 413 maps to AIRequestTooLargeError and context window maps to AIContextExceededError."""
    prov = GroqProvider(api_key="gsk_test")
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")

    resp_413 = httpx.Response(413, request=req, text="Request Entity Too Large")
    with pytest.raises(AIRequestTooLargeError):
        prov._classify_http_error(resp_413)

    resp_ctx = httpx.Response(
        400, request=req, text='{"error": {"message": "context_length_exceeded: maximum context length is 8192"}}'
    )
    with pytest.raises(AIContextExceededError):
        prov._classify_http_error(resp_ctx)

    resp_429 = httpx.Response(
        429, request=req, headers={"retry-after": "5"}, text="Rate limit reached"
    )
    with pytest.raises(AIRateLimitedError) as exc_info:
        prov._classify_http_error(resp_429)
    assert exc_info.value.retry_after_seconds == 5.0

    resp_403 = httpx.Response(403, request=req, text="Access denied for organization")
    with pytest.raises(AIPermissionDeniedError):
        prov._classify_http_error(resp_403)

    resp_401 = httpx.Response(401, request=req, text="Invalid API key provided")
    with pytest.raises(AIAuthFailedError):
        prov._classify_http_error(resp_401)


def test_failover_policy_strict_allowlist():
    """Verify unclassified errors and ValueError produce FAIL_FINAL."""
    act = decide_failover_action(ValueError("Invalid argument in prompt"), attempt=1, max_attempts=1, has_next_candidate=True)
    assert act.action == ActionType.FAIL_FINAL

    act = decide_failover_action(TypeError("Cannot concatenate str to NoneType"), attempt=1, max_attempts=1, has_next_candidate=True)
    assert act.action == ActionType.FAIL_FINAL

    act = decide_failover_action(RuntimeError("Unexpected OS condition"), attempt=1, max_attempts=1, has_next_candidate=True)
    assert act.action == ActionType.FAIL_FINAL

    act = decide_failover_action(AIAuthFailedError("Bad token", provider="groq"), attempt=1, max_attempts=1, has_next_candidate=True)
    assert act.action == ActionType.FAILOVER_NEXT_PROVIDER


def test_fail_final_never_advances_cursor():
    """Verify FAIL_FINAL terminates immediately without advancing job.ai_failover_index."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

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
    db.add(job)
    db.commit()

    def _buggy_fn(p, att):
        raise ValueError("Invalid internal formatting")

    with patch("herald.ai.failover.is_provider_configured", return_value=True), \
         patch("herald.ai.failover.create_provider", return_value=MagicMock()):
        with pytest.raises(ValueError):
            execute_with_failover(
                job=job,
                operation="script",
                execute_fn=_buggy_fn,
                db=db,
                max_same_provider_attempts=1,
            )

    assert job.ai_failover_index == 0


def test_unknown_provider_id_never_becomes_literal():
    """Verify unknown provider ID raises ValueError and is never silently transformed to Literal."""
    assert not is_provider_registered("mystery_ai")
    with pytest.raises(ValueError, match="Unknown AI provider"):
        create_provider("mystery_ai")


def test_resolved_job_settings_to_snapshot():
    """Verify snapshot contains effective resolved values, research identity, and complete chain."""
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
    usr_prefs = {
        "ai_provider_chain_json": ["groq", "cloudflare", "openai"],
    }
    req = {"ai_provider": "gemini"}
    resolved = resolve_job_settings(request_params=req, user_prefs=usr_prefs)
    chain_provs = [c.provider_id for c in resolved.ai_candidates]
    assert chain_provs == ["gemini", "groq", "cloudflare"]


def test_telegram_unconfigured_provider_rejected():
    """Verify unconfigured provider cannot be persisted in Telegram chain."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    with patch("herald.telegram.auth.is_provider_configured", return_value=False):
        with pytest.raises(ValueError, match="not configured"):
            set_user_ai_provider_chain(db, user_id=123, chain=["mistral"])


def test_telegram_duplicate_provider_rejected():
    """Verify duplicate provider selection in Telegram chain is rejected cleanly."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    with patch("herald.telegram.auth.is_provider_configured", return_value=True):
        with pytest.raises(ValueError, match="Duplicate provider"):
            set_user_ai_provider_chain(db, user_id=123, chain=["groq", "openai", "groq"])


def test_telegram_max_3_enforced_in_persistence():
    """Verify persistence layer enforces max 3 providers."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    with pytest.raises(ValueError, match="cannot exceed 3"):
        set_user_ai_provider_chain(db, user_id=123, chain=["groq", "openai", "mistral", "gemini"])


def test_telegram_literal_rules():
    """Verify Literal may only be Primary and sets mode to Literal if AI required."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    with patch("herald.telegram.auth.is_provider_configured", return_value=True):
        with pytest.raises(ValueError, match="Literal provider may only be Primary"):
            set_user_ai_provider_chain(db, user_id=123, chain=["groq", "literal"])

    user = TelegramUser(telegram_user_id=123, telegram_chat_id=123, role="owner", is_active=True, default_mode="standard")
    db.add(user)
    db.commit()

    set_user_ai_provider_chain(db, user_id=123, chain=["literal"])
    db.refresh(user)
    assert user.ai_provider_chain_json == ["literal"]
    assert user.default_mode == "literal"


def test_telegram_model_callback_token_collision_rejection():
    """Verify resolve_model_token rejects ambiguous collisions across models."""
    prov = "groq"
    models = get_models_for_provider(prov)
    if models:
        m = models[0]
        tok = generate_model_token(prov, m.model_id)
        res = resolve_model_token(prov, tok)
        assert res == m.model_id

    with patch("herald.ai.catalog.generate_model_token", return_value="collision123"):
        res = resolve_model_token(prov, "collision123")
        assert res is None


def test_adaptation_leaves_canonical_source_text_unchanged():
    """Verify canonical job.source_text is never modified during adaptation."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    original_text = "Paragraph 1 is very long. " * 500
    job = PodcastJob(
        id="job-adapt-source",
        transport="telegram",
        source_hash="h-src",
        source_text="original",
        status="RECEIVED",
        ai_provider="groq",
        ai_model="llama-3.3-70b-versatile",
        ai_provider_chain_json=[{"provider": "groq", "model": "llama-3.3-70b-versatile"}],
        ai_failover_index=0,
    )
    job.source_text = original_text
    db.add(job)
    db.commit()

    budget = AdaptationBudget(max_ai_calls=5, max_chunks=10)
    usage = AdaptationUsage()

    def fake_distill(chunk, idx, total, provider=None, usage=None, budget=None):
        if usage:
            usage.ai_calls += 1
        return "Distilled summary"

    with patch("herald.ai.adaptation.distill_chunk", side_effect=fake_distill):
        adapted = adapt_source_text(
            source_text=job.source_text,
            budget=budget,
            usage=usage,
            job_id=job.id,
            db=db,
        )

    assert job.source_text == original_text
    assert usage.ai_calls > 0
    assert len(adapted) < len(original_text)


def test_extraction_sanity_metrics_returned():
    """Verify ExtractionResult contains fetched_bytes, extracted_chars, normalized_chars, paragraph_count."""
    fake_html = """
    <html>
      <head><title>Test Article Title</title></head>
      <body>
        <article>
          <p>This is the first paragraph with enough content to be valid article body.</p>
          <p>This is the second paragraph with detailed explanations of the system design.</p>
          <p>This is the third paragraph concluding the analysis of the project requirements.</p>
        </article>
      </body>
    </html>
    """
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"Content-Type": "text/html"}, text=fake_html)
    )
    with patch("herald.extraction.url_extractor.validate_url_host", return_value=("example.com", 443, "93.184.216.34")):
        res = extract_article_from_url("https://example.com/article", transport=transport)

    assert isinstance(res, tuple)
    assert len(res) == 3
    t, b, u = res
    assert t == "Test Article Title"
    assert "first paragraph" in b
    assert hasattr(res, "metrics")
    assert res.metrics["fetched_bytes"] > 0
    assert res.metrics["extracted_chars"] > 0
    assert res.metrics["paragraph_count"] >= 3
    assert isinstance(res.metrics["pollution_detected"], list)


def test_non_gemini_providers_clean_of_gemini_settings():
    """Verify OpenAI, Groq, Cloudflare, Anthropic, Ollama do not read generic GEMINI_* settings."""
    import inspect

    from herald.ai import (
        anthropic_provider,
        cloudflare_provider,
        groq_provider,
        ollama_provider,
        openai_provider,
    )

    modules = [
        anthropic_provider,
        cloudflare_provider,
        groq_provider,
        ollama_provider,
        openai_provider,
    ]
    for mod in modules:
        src = inspect.getsource(mod)
        assert "GEMINI_RETRY_COUNT" not in src, f"{mod.__name__} reads GEMINI_RETRY_COUNT"
        assert "GEMINI_TEMPERATURE" not in src, f"{mod.__name__} reads GEMINI_TEMPERATURE"
        assert "GEMINI_MAX_OUTPUT_TOKENS" not in src, f"{mod.__name__} reads GEMINI_MAX_OUTPUT_TOKENS"


def test_startup_validation_uses_ai_provider():
    """Verify startup validation in api, worker, and telegram_bot uses settings.AI_PROVIDER."""
    import inspect

    from apps.api import main as api_main
    from apps.telegram_bot import main as bot_main
    from apps.worker import main as worker_main

    api_src = inspect.getsource(api_main.validate_server_chain_startup)
    assert "settings.AI_PROVIDER" in api_src
    assert "settings.AI_PRIMARY_PROVIDER" not in api_src

    worker_src = inspect.getsource(worker_main.run_worker_loop)
    assert "settings.AI_PROVIDER" in worker_src
    assert "settings.AI_PRIMARY_PROVIDER" not in worker_src

    bot_src = inspect.getsource(bot_main.main)
    assert "validate_server_default_chain" in bot_src
    assert "settings.AI_PROVIDER" in bot_src


def test_research_through_research_grounding():
    """Verify pipeline.py requires capability 'research_grounding', not 'google_search_grounding'."""
    import inspect

    from herald.core import pipeline

    pipeline_src = inspect.getsource(pipeline)
    assert 'required_capability="research_grounding"' in pipeline_src
    assert "google_search_grounding" not in pipeline_src


def test_adapted_source_forwarded_to_execute_fn():
    """Verify execute_with_failover forwards adapted source_text to execute_fn."""
    chain = [{"provider": "groq", "model": "groq/compound"}]
    job = create_test_job(chain)
    job.source_text = "original_large_text"
    received_sources = []

    def mock_exec(prov, attempt, source_text=None):
        received_sources.append(source_text)
        if attempt == 1:
            raise AIRequestTooLargeError("Source too large")
        return "adapted-success"

    with patch("herald.ai.failover.is_provider_configured", return_value=True), \
         patch("herald.ai.failover.adapt_source_text", return_value="adapted_compact_text"):
        result = execute_with_failover(
            job,
            operation="script_generation",
            execute_fn=mock_exec,
            source_text=job.source_text,
            max_same_provider_attempts=2,
        )

    assert result == "adapted-success"
    assert received_sources == ["original_large_text", "adapted_compact_text"]
    # Invariant: job.source_text remains canonical
    assert job.source_text == "original_large_text"


def test_fail_final_never_advances_cursor_execution():
    """Verify FAIL_FINAL immediately raises without advancing cursor to next candidate."""
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

    # Must only attempt Groq; never advance to Cloudflare or OpenAI
    assert invoked == ["Groq"]
    assert job.ai_failover_index == 0


def test_no_direct_gemini_timeout_uses():
    """Verify gemini/client.py uses effective_ai_timeout_seconds and has no direct GEMINI_TIMEOUT_SECONDS in httpx clients."""
    with open("herald/gemini/client.py", "r", encoding="utf-8") as f:
        content = f.read()

    assert "httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS)" not in content
    assert "timeout=settings.effective_ai_timeout_seconds" in content


def test_no_runtime_gemini_model_writes_in_worker():
    """Verify worker/main.py does not write job.gemini_model for completed jobs."""
    with open("apps/worker/main.py", "r", encoding="utf-8") as f:
        content = f.read()

    assert "job.gemini_model = settings.GEMINI_MODEL" not in content


def test_research_snapshot_survives_env_changes():
    """Verify get_job_ai_identity uses snapshotted research model without rereading settings."""
    from herald.telegram.formatters import get_job_ai_identity

    job = PodcastJob(
        id="job-res-1",
        request_mode="research",
        research_model="gemini-custom-research-v1",
        generation_settings_json={"research_provider": "gemini"},
    )

    with patch("herald.config.settings.GEMINI_RESEARCH_MODEL", "gemini-other-env"):
        prov_name, model_name = get_job_ai_identity(job)

    assert prov_name == "Gemini"
    assert model_name == "gemini-custom-research-v1"


def test_ai_check_stays_inside_user_chain():
    """Verify perform_ai_check inspects capabilities for candidates and has no separate RESEARCH_PROVIDER check."""
    from herald.telegram.bot import perform_ai_check

    mock_client = MagicMock()
    db = MagicMock()

    with patch("herald.telegram.bot.get_effective_user_preferences", return_value={}), \
         patch("herald.telegram.bot.get_ai_provider") as mock_get_p:
        mock_prov = MagicMock()
        mock_prov.check_connection.return_value = {"connected": True}
        mock_get_p.return_value = mock_prov
        perform_ai_check(db, mock_client, chat_id=123, user_id=456)

    assert mock_client.send_message.call_count >= 2
    final_text = mock_client.send_message.call_args_list[-1][1]["text"]
    assert "Your Failover Chain:" in final_text
    assert "Research Grounding:" in final_text
    # Capabilities reported in output
    assert "Capabilities:" in final_text


def test_provider_distillation_actually_runs():
    """Verify distill_chunk invokes provider.distill_text and propagates typed errors."""
    from herald.ai.adaptation import distill_chunk

    mock_prov = MagicMock()
    mock_prov.distill_text.return_value = "Structured distilled text"

    res = distill_chunk("Long text chunk", chunk_index=0, total_chunks=1, provider=mock_prov)
    assert res == "Structured distilled text"
    mock_prov.distill_text.assert_called_once()

    # Verify typed errors are NOT silently swallowed
    mock_failing_prov = MagicMock()
    mock_failing_prov.distill_text.side_effect = AIRateLimitedError("Rate limit in distillation")

    with pytest.raises(AIRateLimitedError):
        distill_chunk("Long text chunk", chunk_index=0, total_chunks=1, provider=mock_failing_prov)


def test_unknown_discovered_models_have_unknown_limits():
    """Verify live discovery sets context_window=None, max_output=None for unknown models."""
    prov = OpenAIProvider(api_key="sk-test")
    fake_models_resp = {
        "data": [
            {"id": "unknown-brand-new-model-2026"},
        ]
    }

    with patch("httpx.Client.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_models_resp
        mock_get.return_value = mock_resp

        discovered = prov.discover_models()

    assert len(discovered) == 1
    m = discovered[0]
    assert m.model_id == "unknown-brand-new-model-2026"
    assert m.context_window is None
    assert m.max_output is None


def test_configured_secondary_keeps_ai_mode_available_when_primary_unconfigured():
    """When Primary is unconfigured but Secondary has valid credentials, AI mode is available."""
    from herald.ai.resolution import resolve_job_settings

    # Primary (gemini) unconfigured, Secondary (groq) configured
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
    from herald.ai.resolution import get_server_default_chain

    mock_cfg = MagicMock()
    mock_cfg.AI_PROVIDER = "groq"
    mock_cfg.AI_SECONDARY_PROVIDER = "groq"  # Duplicate -> invalid chain
    mock_cfg.AI_TERTIARY_PROVIDER = None

    with pytest.raises(ValueError) as exc:
        get_server_default_chain(mock_cfg)

    assert "Invalid server default AI provider chain" in str(exc.value)


def test_config_no_duplicate_adaptation_block():
    """Verify Settings has only one ADAPTATION_* block containing ADAPTATION_CHUNK_MAX_CHARS."""
    with open("herald/config.py", "r", encoding="utf-8") as f:
        lines = f.readlines()

    adaptation_chunk_lines = [line for line in lines if "ADAPTATION_CHUNK_MAX_CHARS" in line]
    assert len(adaptation_chunk_lines) == 1

    adaptation_max_chunks_lines = [line for line in lines if "ADAPTATION_MAX_CHUNKS" in line]
    assert len(adaptation_max_chunks_lines) == 1

