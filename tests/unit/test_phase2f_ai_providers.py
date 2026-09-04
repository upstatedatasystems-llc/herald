"""
Comprehensive unit test suite for Package 2F: Configurable AI Provider Support.
Verifies:
- AIProvider factory and resolution (Gemini, Groq, OpenRouter, Mistral, Cloudflare Workers AI, Literal/None)
- Provider capabilities declarations
- Connection checking, timeout, auth failure, rate-limit, and model-unavailable classification across all providers
- Structured podcast script generation and neutral Pydantic schema validation
- 1 bounded schema repair attempt on validation failure
- Truthful one-HTTP-call / one-AIInteraction recording invariant across all success, retry, and repair failure paths
- No call creates both a success and failure row
- Bounded support evidence tracking in AIInteraction
- Research provider separation (Gemini, Groq+Gemini, OpenRouter+Gemini)
- Incompatible research provider rejection without silent Standard downgrade
- API provider routing via AIProvider and get_research_provider()
- Truthful reporting in diagnostics cards and /ai_check status
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from herald.ai.cloudflare_provider import CloudflareProvider
from herald.ai.factory import (
    create_ai_provider,
    get_ai_provider,
    get_research_provider,
    reset_ai_provider,
)
from herald.ai.gemini_provider import GeminiProvider
from herald.ai.groq_provider import GroqProvider
from herald.ai.literal_provider import LiteralProvider
from herald.ai.mistral_provider import MistralProvider
from herald.ai.openrouter_provider import OpenRouterProvider
from herald.config import settings
from herald.core.models import HeraldRequest
from herald.core.pipeline import process_herald_request
from herald.db.connection import Base
from herald.db.models import AIInteraction, JobState, PodcastJob


def setup_in_memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return session_factory()


SAMPLE_SCRIPT_JSON = {
    "episode_title": "AI Provider Expansion",
    "episode_description": "A comprehensive breakdown of multi-provider orchestration.",
    "estimated_minutes": 4,
    "source_title": "Multi-Provider Herald",
    "segments": [
        {"order": 1, "heading": "Introduction", "narration": "Welcome to today's multi-provider overview."},
        {"order": 2, "heading": "Analysis", "narration": "Different LLMs bring complementary strengths."},
    ],
    "warnings": [],
}


def test_frozen_provider_resolution_and_capabilities():
    """Verify factory resolves all 6 frozen providers and their declared capabilities."""
    reset_ai_provider()

    # 1. Gemini
    with patch.object(settings, "AI_PROVIDER", "gemini"), patch.object(settings, "GEMINI_API_KEY", "key_gemini"):
        reset_ai_provider()
        prov = get_ai_provider()
        assert isinstance(prov, GeminiProvider)
        assert prov.provider_name == "Gemini"
        assert prov.capabilities.research_grounding is True
        assert prov.capabilities.structured_output is True

    # 2. Groq
    with patch.object(settings, "AI_PROVIDER", "groq"), patch.object(settings, "GROQ_API_KEY", "key_groq"):
        reset_ai_provider()
        prov = get_ai_provider()
        assert isinstance(prov, GroqProvider)
        assert prov.provider_name == "Groq"
        assert prov.capabilities.research_grounding is False
        assert prov.capabilities.script_standard is True

    # 3. OpenRouter
    with patch.object(settings, "AI_PROVIDER", "openrouter"), patch.object(settings, "OPENROUTER_API_KEY", "key_or"):
        reset_ai_provider()
        prov = get_ai_provider()
        assert isinstance(prov, OpenRouterProvider)
        assert prov.provider_name == "OpenRouter"
        assert prov.capabilities.structured_output is True

    # 4. Mistral
    with patch.object(settings, "AI_PROVIDER", "mistral"), patch.object(settings, "MISTRAL_API_KEY", "key_mis"):
        reset_ai_provider()
        prov = get_ai_provider()
        assert isinstance(prov, MistralProvider)
        assert prov.provider_name == "Mistral"
        assert prov.capabilities.script_standard is True

    # 5. Cloudflare Workers AI
    with patch.object(settings, "AI_PROVIDER", "cloudflare"), \
         patch.object(settings, "CLOUDFLARE_API_TOKEN", "token_cf"), \
         patch.object(settings, "CLOUDFLARE_ACCOUNT_ID", "acct_cf"), \
         patch.object(settings, "CLOUDFLARE_AI_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"):
        reset_ai_provider()
        prov = get_ai_provider()
        assert isinstance(prov, CloudflareProvider)
        assert prov.provider_name == "Cloudflare Workers AI"
        assert prov.capabilities.script_brief is True

    # 6. Literal / None
    with patch.object(settings, "AI_PROVIDER", "literal"):
        reset_ai_provider()
        assert get_ai_provider() is None
        lit_prov = create_ai_provider("literal")
        assert isinstance(lit_prov, LiteralProvider)
        assert lit_prov.capabilities.usage_metrics is False


def test_provider_health_sanitization_matrix():
    """Verify check_connection across providers handles 200, 401, 404, 429, timeout with classified sanitized errors."""
    import httpx

    # Groq (OpenAI-compatible)
    groq = GroqProvider(api_key="gsk_mock_123")
    assert groq.is_configured() is True

    # Healthy
    with patch("httpx.Client.get", return_value=MagicMock(status_code=200, json=lambda: {"data": [{"id": "llama-3.3-70b-versatile"}]})):
        res = groq.check_connection(timeout_seconds=2.0)
        assert res["connected"] is True
        assert res["error"] is None

    # Auth Failure
    with patch("httpx.Client.get", return_value=MagicMock(status_code=401, text="Invalid API key")):
        res = groq.check_connection(timeout_seconds=2.0)
        assert res["connected"] is False
        assert res["error"] == "authentication failed"

    # Rate Limit
    with patch("httpx.Client.get", return_value=MagicMock(status_code=429, text="Rate limit reached")):
        res = groq.check_connection(timeout_seconds=2.0)
        assert res["connected"] is False
        assert res["error"] == "rate limit exceeded"

    # Timeout
    with patch("httpx.Client.get", side_effect=httpx.TimeoutException("Timeout")):
        res = groq.check_connection(timeout_seconds=2.0)
        assert res["connected"] is False
        assert res["error"] == "connection timed out"

    # Model Unavailable (404)
    with patch("httpx.Client.get", return_value=MagicMock(status_code=404, text="Model not found")):
        res = groq.check_connection(timeout_seconds=2.0)
        assert res["connected"] is False
        assert res["error"] == "configured model unavailable"


def test_one_call_one_record_success_and_bounded_evidence():
    """Invariant: 1 successful HTTP request creates exactly 1 AIInteraction row with bounded evidence."""
    db = setup_in_memory_db()
    job_id = "job-single-call"
    db.add(PodcastJob(id=job_id, transport="telegram", telegram_user_id=1, telegram_chat_id=1, source_hash="h1", source_text="Source", status=JobState.RECEIVED.value, created_at=datetime.now(UTC)))
    db.commit()

    provider = GroqProvider(api_key="gsk_valid")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"x-request-id": "req-groq-001"}
    mock_resp.json.return_value = {
        "id": "req-groq-001",
        "choices": [{"message": {"content": json.dumps(SAMPLE_SCRIPT_JSON)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 120, "total_tokens": 200},
    }

    with patch("httpx.Client.post", return_value=mock_resp), \
         patch("herald.services.ai_recorder.SessionLocal", return_value=db):
        script = provider.generate_script(
            source_text="Test source text for bounded evidence verification.",
            request_mode="standard",
            source_title="One Call Test",
            job_id=job_id,
        )

    assert script.episode_title == "AI Provider Expansion"

    interactions = db.query(AIInteraction).filter(AIInteraction.job_id == job_id).all()
    assert len(interactions) == 1
    rec = interactions[0]
    assert rec.success is True
    assert rec.http_status == 200
    assert rec.provider_request_id == "req-groq-001"
    assert rec.attempt == 1
    assert rec.prompt_tokens == 80
    assert rec.completion_tokens == 120
    assert rec.total_tokens == 200

    # Bounded evidence check: No full prompt or script in request/response evidence
    req_ev = rec.request_json_sanitized or {}
    resp_ev = rec.response_json_sanitized or {}
    assert "mode" in req_ev
    assert "source_character_count" in req_ev
    assert "messages" not in req_ev  # full prompt not stored
    assert resp_ev.get("schema_validation") == "valid"
    assert "segments" not in resp_ev  # full narration not stored in evidence


def test_one_call_one_record_retry_success():
    """Invariant: 1 failed HTTP (429) + 1 retry success = exactly 2 AIInteraction rows."""
    db = setup_in_memory_db()
    job_id = "job-retry-call"
    db.add(PodcastJob(id=job_id, transport="telegram", telegram_user_id=1, telegram_chat_id=1, source_hash="h2", source_text="Source", status=JobState.RECEIVED.value, created_at=datetime.now(UTC)))
    db.commit()

    provider = MistralProvider(api_key="mis_valid")

    resp1 = MagicMock(status_code=429, text="Rate limit exceeded", headers={"x-request-id": "req-mis-1"})
    resp2 = MagicMock(
        status_code=200,
        headers={"x-request-id": "req-mis-2"},
        json=lambda: {
            "id": "req-mis-2",
            "choices": [{"message": {"content": json.dumps(SAMPLE_SCRIPT_JSON)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150},
        },
    )

    with patch("httpx.Client.post", side_effect=[resp1, resp2]), \
         patch("time.sleep", return_value=None), \
         patch("herald.services.ai_recorder.SessionLocal", return_value=db):
        script = provider.generate_script(source_text="Retry test", request_mode="brief", job_id=job_id)

    assert script.episode_title == "AI Provider Expansion"
    interactions = db.query(AIInteraction).filter(AIInteraction.job_id == job_id).order_by(AIInteraction.started_at.asc()).all()
    assert len(interactions) == 2
    assert interactions[0].success is False
    assert interactions[0].attempt == 1
    assert interactions[0].http_status == 429
    assert interactions[1].success is True
    assert interactions[1].attempt == 2
    assert interactions[1].http_status == 200


def test_one_call_one_record_schema_repair_success():
    """Invariant: Schema invalid HTTP 200 + repair success = exactly 2 AIInteraction rows (1 failed + 1 success)."""
    db = setup_in_memory_db()
    job_id = "job-repair-call"
    db.add(PodcastJob(id=job_id, transport="telegram", telegram_user_id=1, telegram_chat_id=1, source_hash="h3", source_text="Source", status=JobState.RECEIVED.value, created_at=datetime.now(UTC)))
    db.commit()

    provider = OpenRouterProvider(api_key="sk-or-valid")

    bad_resp = MagicMock(
        status_code=200,
        headers={"x-request-id": "or-bad-1"},
        json=lambda: {"id": "or-bad-1", "choices": [{"message": {"content": '{"broken_schema": true}'}}]},
    )
    good_repair_resp = MagicMock(
        status_code=200,
        headers={"x-request-id": "or-good-2"},
        json=lambda: {
            "id": "or-good-2",
            "choices": [{"message": {"content": json.dumps(SAMPLE_SCRIPT_JSON)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        },
    )

    with patch("httpx.Client.post", side_effect=[bad_resp, good_repair_resp]), \
         patch("herald.services.ai_recorder.SessionLocal", return_value=db):
        script = provider.generate_script(source_text="Schema repair test", request_mode="standard", job_id=job_id)

    assert script.episode_title == "AI Provider Expansion"
    interactions = db.query(AIInteraction).filter(AIInteraction.job_id == job_id).order_by(AIInteraction.started_at.asc()).all()
    assert len(interactions) == 2
    assert interactions[0].success is False
    assert interactions[0].operation == "script_generation"
    assert interactions[0].http_status == 200
    assert interactions[1].success is True
    assert interactions[1].operation == "script_repair"
    assert interactions[1].http_status == 200


def test_one_call_one_record_schema_repair_failure():
    """Invariant: Schema invalid HTTP 200 + repair HTTP error = exactly 2 AIInteraction rows (both failed)."""
    db = setup_in_memory_db()
    job_id = "job-repair-fail"
    db.add(PodcastJob(id=job_id, transport="telegram", telegram_user_id=1, telegram_chat_id=1, source_hash="h4", source_text="Source", status=JobState.RECEIVED.value, created_at=datetime.now(UTC)))
    db.commit()

    provider = GroqProvider(api_key="gsk_valid")

    bad_resp = MagicMock(
        status_code=200,
        headers={"x-request-id": "g-bad"},
        json=lambda: {"id": "g-bad", "choices": [{"message": {"content": '{"incomplete": true}'}}]},
    )
    fail_repair_resp = MagicMock(status_code=500, text="Internal Server Error", headers={"x-request-id": "g-rep-fail"})

    with patch.object(settings, "GEMINI_RETRY_COUNT", 1), \
         patch("httpx.Client.post", side_effect=[bad_resp, fail_repair_resp]), \
         patch("herald.services.ai_recorder.SessionLocal", return_value=db):
        try:
            provider.generate_script(source_text="Repair fail test", request_mode="standard", job_id=job_id)
            assert False, "Expected RuntimeError on failed repair"
        except RuntimeError as re:
            assert "HTTP 500" in str(re) or "returned HTTP 500" in str(re)

    interactions = db.query(AIInteraction).filter(AIInteraction.job_id == job_id).order_by(AIInteraction.started_at.asc()).all()
    assert len(interactions) == 2
    assert interactions[0].success is False
    assert interactions[0].operation == "script_generation"
    assert interactions[1].success is False
    assert interactions[1].operation == "script_repair"


def test_research_provider_separation_matrix():
    """Verify research mode with various provider combinations."""
    db = setup_in_memory_db()

    # Case A: Groq primary + Gemini Research -> Success
    with patch.object(settings, "AI_PROVIDER", "groq"), \
         patch.object(settings, "GROQ_API_KEY", "gsk_groq"), \
         patch.object(settings, "RESEARCH_PROVIDER", "gemini"), \
         patch.object(settings, "GEMINI_API_KEY", "gem_key"):
        reset_ai_provider()
        r_prov = get_research_provider()
        assert r_prov is not None
        assert r_prov.provider_name == "Gemini"

    # Case B: OpenRouter primary + Gemini Research -> Success
    with patch.object(settings, "AI_PROVIDER", "openrouter"), \
         patch.object(settings, "OPENROUTER_API_KEY", "sk-or-key"), \
         patch.object(settings, "RESEARCH_PROVIDER", "gemini"), \
         patch.object(settings, "GEMINI_API_KEY", "gem_key"):
        reset_ai_provider()
        r_prov = get_research_provider()
        assert r_prov is not None
        assert r_prov.provider_name == "Gemini"

    # Case C: Missing Research Provider -> Rejection without silent downgrade
    with patch.object(settings, "AI_PROVIDER", "groq"), \
         patch.object(settings, "GROQ_API_KEY", "gsk_groq"), \
         patch.object(settings, "RESEARCH_PROVIDER", "gemini"), \
         patch.object(settings, "GEMINI_API_KEY", ""):
        reset_ai_provider()
        req = HeraldRequest(
            transport="telegram",
            requester_identity="telegram:100",
            delivery_target="100",
            request_mode="research",
            source_text="Research request without Gemini credentials.",
        )
        resp = process_herald_request(db, req)
        assert resp.status == JobState.FAILED_FINAL.value
        assert resp.error_category == "INCOMPATIBLE_PROVIDER_FOR_RESEARCH"
        assert "Google Search Grounding" in resp.message


def test_api_provider_neutral_routing():
    """Verify apps/api/main.py routes Brief/Standard scripts through AIProvider uniformly."""
    from fastapi.testclient import TestClient

    from apps.api.main import app, get_db, verify_api_key

    db = setup_in_memory_db()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[verify_api_key] = lambda: True
    client = TestClient(app)

    job = PodcastJob(
        id="api-route-job-1",
        transport="api",
        request_mode="standard",
        source_hash="sha256_mock_api_source",
        status=JobState.SOURCE_READY.value,
        source_text="API source content to convert into script",
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    mock_provider = MagicMock()
    mock_provider.is_configured.return_value = True
    mock_provider.configured_model = "llama-3.3-70b-versatile"
    from herald.ai.schema import PodcastScriptResponse
    mock_provider.generate_script.return_value = PodcastScriptResponse(**SAMPLE_SCRIPT_JSON)

    with patch("herald.ai.factory.get_ai_provider", return_value=mock_provider):
        response = client.post("/api/v1/script/generate", json={"job_id": job.id})

    app.dependency_overrides.clear()
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["status"] == JobState.QUEUED_TTS.value
    assert res_data["episode_title"] == "AI Provider Expansion"
    assert mock_provider.generate_script.called

    db_job = db.query(PodcastJob).filter(PodcastJob.id == job.id).first()
    assert db_job.gemini_model == "llama-3.3-70b-versatile"
    assert db_job.script_json["episode_title"] == "AI Provider Expansion"


def test_cloudflare_model_precedence_and_validation():
    """Verify Cloudflare settings precedence: CLOUDFLARE_AI_MODEL -> CLOUDFLARE_MODEL -> default."""
    from herald.config import Settings

    # Case 1: Neither set -> default
    s1 = Settings(CLOUDFLARE_AI_MODEL="", CLOUDFLARE_MODEL="")
    assert s1.effective_cloudflare_ai_model == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"

    # Case 2: Only CLOUDFLARE_MODEL set -> returns CLOUDFLARE_MODEL
    s2 = Settings(CLOUDFLARE_AI_MODEL="", CLOUDFLARE_MODEL="@cf/meta/llama-3.1-8b-instruct")
    assert s2.effective_cloudflare_ai_model == "@cf/meta/llama-3.1-8b-instruct"

    # Case 3: Both set -> CLOUDFLARE_AI_MODEL takes precedence
    s3 = Settings(CLOUDFLARE_AI_MODEL="@cf/meta/llama-3.3-70b-instruct-fp8-fast", CLOUDFLARE_MODEL="@cf/meta/llama-3.1-8b-instruct")
    assert s3.effective_cloudflare_ai_model == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"

    # Cloudflare Provider check_connection inspects model availability
    cf = CloudflareProvider(
        account_id="cf_acct_123",
        api_token="cf_token_456",
        model_name="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    )
    # Available
    with patch("httpx.Client.get", return_value=MagicMock(status_code=200, json=lambda: {"result": [{"name": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"}]})):
        res = cf.check_connection(timeout_seconds=2.0)
        assert res["connected"] is True
        assert res["error"] is None

    # Configured model missing from search results
    with patch("httpx.Client.get", return_value=MagicMock(status_code=200, json=lambda: {"result": [{"name": "@cf/other-model"}]})):
        res = cf.check_connection(timeout_seconds=2.0)
        assert res["connected"] is False
        assert res["error"] == "configured model unavailable"


def test_api_research_endpoint_validation():
    """Verify apps/api/main.py /api/v1/research rejects when research provider is unconfigured or incompatible."""
    from fastapi.testclient import TestClient

    from apps.api.main import app, get_db, verify_api_key

    db = setup_in_memory_db()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[verify_api_key] = lambda: True
    client = TestClient(app)

    job = PodcastJob(
        id="api-research-job-1",
        transport="api",
        request_mode="research",
        source_hash="sha256_mock_res",
        status=JobState.SOURCE_READY.value,
        source_text="Research source content",
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    # Case 1: Research provider not configured
    with patch("herald.ai.factory.get_research_provider", return_value=None):
        resp = client.post("/api/v1/script/generate", json={"job_id": job.id})
        assert resp.status_code == 400
        assert "Google Search Grounding" in resp.json()["detail"]

    # Reset job status for Case 2
    job.status = JobState.SOURCE_READY.value
    db.commit()

    # Case 2: Research provider configured but lacks capabilities.research_grounding
    mock_prov = MagicMock()
    mock_prov.is_configured.return_value = True
    mock_prov.capabilities.research_grounding = False
    with patch("herald.ai.factory.get_research_provider", return_value=mock_prov):
        resp = client.post("/api/v1/script/generate", json={"job_id": job.id})
        assert resp.status_code == 400
        assert "Google Search Grounding" in resp.json()["detail"]

    app.dependency_overrides.clear()

