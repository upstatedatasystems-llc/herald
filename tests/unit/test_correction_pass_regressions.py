import json
from unittest.mock import MagicMock, patch
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.ai.adaptation import AdaptationBudget, AdaptationUsage, adapt_source_text
from herald.ai.capabilities import AIModelCapabilities, ProviderCapabilities
from herald.ai.catalog import (
    generate_model_token,
    get_models_for_provider,
    resolve_model_token,
    validate_model_for_provider,
)
from herald.ai.cloudflare_provider import CloudflareProvider, extract_cloudflare_content
from herald.ai.errors import (
    AIAuthFailedError,
    AIChainExhaustedError,
    AIClientTimeoutError,
    AIContextExceededError,
    AIPermissionDeniedError,
    AIProviderError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
    AIRateLimitedError,
    AIRequestTooLargeError,
    AISchemaInvalidError,
)
from herald.ai.failover import execute_with_failover
from herald.ai.groq_provider import GroqProvider
from herald.ai.openai_provider import OpenAIProvider
from herald.ai.policy import ActionType, decide_failover_action
from herald.ai.literal_provider import LiteralProvider
from herald.ai.registry import (
    create_provider,
    get_descriptor,
    is_provider_configured,
    is_provider_registered,
)
from herald.ai.resolution import (
    AIProviderCandidate,
    ResolvedJobSettings,
    resolve_job_settings,
)
from herald.config import settings
from herald.db.models import Base, PodcastJob, TelegramUser
from herald.extraction.url_extractor import ExtractionResult, extract_article_from_url
from herald.telegram.auth import (
    get_effective_user_preferences,
    set_user_ai_model_for_provider,
    set_user_ai_provider_chain,
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
    """Verify Qwen and Gemma request payloads contain authoritative catalog metadata."""
    prov_qwen = CloudflareProvider(
        account_id="acc",
        api_token="tok",
        model="@cf/qwen/qwen2.5-72b-instruct",
    )
    payload_qwen = prov_qwen._build_request_payload(
        system_prompt="sys",
        user_prompt="usr",
        max_output_tokens=16384,
    )
    assert payload_qwen["max_tokens"] == 16384
    assert payload_qwen["reasoning_effort"] == "low"

    prov_gemma = CloudflareProvider(
        account_id="acc",
        api_token="tok",
        model="@cf/google/gemma-7b-it",
    )
    payload_gemma = prov_gemma._build_request_payload(
        system_prompt="sys",
        user_prompt="usr",
        max_output_tokens=8192,
    )
    assert payload_gemma["max_tokens"] == 8192


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
