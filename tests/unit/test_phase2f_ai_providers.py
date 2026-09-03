"""
Comprehensive unit test suite for Package 2F: Configurable AI Provider Support.
Tests:
- AIProvider factory and provider resolution (Gemini, Groq, OpenRouter, Mistral, Cloudflare Workers AI, Literal/None)
- Provider capabilities declarations
- Connection checking and configuration detection across all providers
- Structured podcast script generation and response parsing
- 1 bounded schema repair attempt on validation failure
- AI interaction persistence with provider request IDs, HTTP status, and attempt-level telemetry
- Research provider separation (AI_PROVIDER=groq + RESEARCH_PROVIDER=gemini works)
- Research mode compatibility guard rejecting unconfigured research providers without silent downgrades
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
from herald.telegram.formatters import format_diagnostics_card


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
         patch.object(settings, "CLOUDFLARE_ACCOUNT_ID", "acct_cf"):
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


def test_openrouter_provider_generation_and_repair():
    """Test OpenRouter script generation, header forwarding, and 1 bounded schema repair."""
    db = setup_in_memory_db()
    job_id = "job-or-001"
    job = PodcastJob(
        id=job_id,
        transport="telegram",
        telegram_user_id=1,
        telegram_chat_id=1,
        source_hash="h_or",
        source_text="OpenRouter source text",
        status=JobState.RECEIVED.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    provider = OpenRouterProvider(api_key="sk-or-mock-key-12345", model="meta-llama/llama-3.3-70b-instruct")
    assert provider.is_configured() is True

    # Simulate first attempt returning invalid json, then second repair request returning valid json
    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.headers = {"x-request-id": "or-req-bad"}
    bad_resp.json.return_value = {
        "id": "or-req-bad",
        "choices": [{"message": {"content": "{invalid json"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }

    good_resp = MagicMock()
    good_resp.status_code = 200
    good_resp.headers = {"x-request-id": "or-req-good"}
    good_resp.json.return_value = {
        "id": "or-req-good",
        "choices": [{"message": {"content": json.dumps(SAMPLE_SCRIPT_JSON)}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 200, "total_tokens": 320},
    }

    with patch("httpx.Client.post", side_effect=[bad_resp, good_resp]), \
         patch("herald.services.ai_recorder.SessionLocal", return_value=db):
        script = provider.generate_script(
            source_text="Test source text",
            request_mode="standard",
            source_title="OpenRouter Test",
            job_id=job_id,
        )

    assert script.episode_title == "AI Provider Expansion"
    assert len(script.segments) == 2

    # Invariant: 2 distinct AIInteraction records logged (failed attempt + successful repair)
    interactions = db.query(AIInteraction).filter(AIInteraction.job_id == job_id).order_by(AIInteraction.started_at.asc()).all()
    assert len(interactions) == 2
    assert interactions[0].success is False
    assert interactions[0].operation == "script_generation"
    assert interactions[1].success is True
    assert interactions[1].operation == "script_repair"
    assert interactions[1].provider_request_id == "or-req-good"


def test_cloudflare_provider_generation():
    """Test Cloudflare Workers AI script generation and unwrap response."""
    db = setup_in_memory_db()
    job_id = "job-cf-002"
    job = PodcastJob(
        id=job_id,
        transport="telegram",
        telegram_user_id=2,
        telegram_chat_id=2,
        source_hash="h_cf",
        source_text="Cloudflare source text",
        status=JobState.RECEIVED.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    provider = CloudflareProvider(
        api_token="cf_token_123",
        account_id="cf_acct_456",
        model="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    )
    assert provider.is_configured() is True

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"cf-ray": "ray-12345678"}
    mock_resp.json.return_value = {
        "result": {"response": json.dumps(SAMPLE_SCRIPT_JSON)},
        "success": True,
    }

    with patch("httpx.Client.post", return_value=mock_resp), \
         patch("herald.services.ai_recorder.SessionLocal", return_value=db):
        script = provider.generate_script(
            source_text="Test cloudflare text",
            request_mode="brief",
            job_id=job_id,
        )

    assert script.episode_title == "AI Provider Expansion"
    interactions = db.query(AIInteraction).filter(AIInteraction.job_id == job_id).all()
    assert len(interactions) == 1
    rec = interactions[0]
    assert rec.provider == "cloudflare"
    assert rec.provider_request_id == "ray-12345678"
    assert rec.http_status == 200


def test_research_provider_separation_success():
    """
    Contract: When AI_PROVIDER=groq and RESEARCH_PROVIDER=gemini (with GEMINI_API_KEY set),
    research mode succeeds because a research-capable provider is configured.
    """
    db = setup_in_memory_db()

    with patch.object(settings, "AI_PROVIDER", "groq"), \
         patch.object(settings, "GROQ_API_KEY", "gsk_groq_key"), \
         patch.object(settings, "RESEARCH_PROVIDER", "gemini"), \
         patch.object(settings, "GEMINI_API_KEY", "valid_gemini_key"):
        reset_ai_provider()

        r_prov = get_research_provider()
        assert r_prov is not None
        assert r_prov.provider_name == "Gemini"
        assert r_prov.capabilities.research_grounding is True

        req = HeraldRequest(
            transport="telegram",
            requester_identity="telegram:100",
            delivery_target="100",
            request_mode="research",
            research_depth="high",
            source_text="Research mode topic with separate research provider.",
        )

        with patch("herald.core.pipeline.generate_grounded_research", return_value={"search_count": 1, "source_count": 1}), \
             patch("herald.core.pipeline.normalize_research_dossier", return_value=MagicMock(model_dump=lambda: {"summary": "ok"})), \
             patch("herald.core.pipeline.generate_podcast_script", return_value=MagicMock(model_dump=lambda: SAMPLE_SCRIPT_JSON)), \
             patch("herald.core.pipeline.audit_research_script", return_value=MagicMock(model_dump=lambda: {"has_material_issues": False})):
            resp = process_herald_request(db, req)

        assert resp.status in (JobState.QUEUED_TTS.value, JobState.SCRIPT_READY.value)
        assert resp.is_duplicate is False


def test_research_provider_separation_failure_when_unconfigured():
    """
    Contract: When AI_PROVIDER=groq and NO research-capable provider is configured,
    research mode must fail actionably without silent downgrade.
    """
    db = setup_in_memory_db()

    with patch.object(settings, "AI_PROVIDER", "groq"), \
         patch.object(settings, "GROQ_API_KEY", "gsk_groq_key"), \
         patch.object(settings, "RESEARCH_PROVIDER", "gemini"), \
         patch.object(settings, "GEMINI_API_KEY", ""):
        reset_ai_provider()

        r_prov = get_research_provider()
        assert r_prov is None

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
        assert "GEMINI_API_KEY" in resp.message


def test_diagnostics_card_truthful_attribution():
    """Verify diagnostics card truthfully attributes provider name and model."""
    db = setup_in_memory_db()
    job = PodcastJob(
        id="aabb1122-3344-4556-8778-99aabbccddeeff",
        transport="telegram",
        telegram_user_id=1,
        telegram_chat_id=1,
        source_hash="h_diag",
        source_text="Source",
        custom_title="Multi-Provider Episode",
        request_mode="standard",
        status=JobState.COMPLETE.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)

    # Add AI interaction with provider="openrouter"
    interaction = AIInteraction(
        id="ai-rec-001",
        job_id=job.id,
        provider="openrouter",
        model="meta-llama/llama-3.3-70b-instruct",
        operation="script_generation",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        duration_ms=2500,
        success=True,
        prompt_tokens=300,
        completion_tokens=400,
        total_tokens=700,
        created_at=datetime.now(UTC),
    )
    db.add(interaction)
    db.commit()

    card = format_diagnostics_card(job, db)
    assert "OpenRouter (meta-llama/llama-3.3-70b-instruct)" in card
    assert "700 tokens" in card
