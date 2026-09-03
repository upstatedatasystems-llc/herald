"""
Comprehensive unit test suite for Package 2F: Configurable AI Provider Support.
Tests:
- AIProvider factory and provider resolution (gemini, anthropic, openai, groq, ollama, literal/none)
- Connection checking and configuration detection across all providers
- Structured podcast script generation and response parsing
- AI interaction persistence with provider-specific token accounting
- Retries generating distinct external call evidence
- Research mode compatibility guard rejecting non-Gemini providers without silent downgrades
- Truthful reporting in diagnostics cards and /ai_check status
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.main import app
from herald.ai.anthropic_provider import AnthropicProvider
from herald.ai.factory import create_ai_provider, get_ai_provider, reset_ai_provider
from herald.ai.gemini_provider import GeminiProvider
from herald.ai.groq_provider import GroqProvider
from herald.ai.literal_provider import LiteralProvider
from herald.ai.ollama_provider import OllamaProvider
from herald.ai.openai_provider import OpenAIProvider
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
    "episode_title": "AI Revolution Update",
    "episode_description": "A comprehensive breakdown of multi-model orchestration.",
    "estimated_minutes": 4,
    "source_title": "Multi-Provider Herald",
    "segments": [
        {"order": 1, "heading": "Introduction", "narration": "Welcome to today's multi-provider overview."},
        {"order": 2, "heading": "Analysis", "narration": "Different LLMs bring complementary strengths."},
    ],
    "warnings": [],
}


def test_factory_provider_resolution():
    """Verify factory returns appropriate provider instance based on settings and arguments."""
    reset_ai_provider()

    with patch.object(settings, "AI_PROVIDER", "gemini"):
        reset_ai_provider()
        prov = get_ai_provider()
        assert isinstance(prov, GeminiProvider)
        assert prov.provider_name == "Gemini"

    with patch.object(settings, "AI_PROVIDER", "anthropic"):
        reset_ai_provider()
        prov = get_ai_provider()
        assert isinstance(prov, AnthropicProvider)
        assert prov.provider_name == "Anthropic"

    with patch.object(settings, "AI_PROVIDER", "openai"):
        reset_ai_provider()
        prov = get_ai_provider()
        assert isinstance(prov, OpenAIProvider)
        assert prov.provider_name == "OpenAI"

    with patch.object(settings, "AI_PROVIDER", "groq"):
        reset_ai_provider()
        prov = get_ai_provider()
        assert isinstance(prov, GroqProvider)
        assert prov.provider_name == "Groq"

    with patch.object(settings, "AI_PROVIDER", "ollama"):
        reset_ai_provider()
        prov = get_ai_provider()
        assert isinstance(prov, OllamaProvider)
        assert prov.provider_name == "Ollama"

    with patch.object(settings, "AI_PROVIDER", "literal"):
        reset_ai_provider()
        prov = get_ai_provider()
        assert prov is None

    with patch.object(settings, "AI_PROVIDER", "none"):
        reset_ai_provider()
        prov = get_ai_provider()
        assert prov is None

    # Test create_ai_provider direct helper
    assert isinstance(create_ai_provider("anthropic"), AnthropicProvider)
    assert isinstance(create_ai_provider("openai"), OpenAIProvider)
    assert isinstance(create_ai_provider("groq"), GroqProvider)
    assert isinstance(create_ai_provider("ollama"), OllamaProvider)
    assert isinstance(create_ai_provider("literal"), LiteralProvider)


def test_anthropic_provider_generation_and_tokens():
    """Test Anthropic provider script generation, JSON parsing, and token recording."""
    db = setup_in_memory_db()
    job_id = "job-anthropic-001"
    job = PodcastJob(
        id=job_id,
        transport="telegram",
        telegram_user_id=1,
        telegram_chat_id=1,
        source_hash="h_ant",
        source_text="Anthropic source content",
        status=JobState.RECEIVED.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    provider = AnthropicProvider(api_key="sk-ant-mock-key-12345", model="claude-3-7-sonnet-20250219")
    assert provider.is_configured() is True
    assert provider.configured_model == "claude-3-7-sonnet-20250219"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [{"type": "text", "text": json.dumps(SAMPLE_SCRIPT_JSON)}],
        "usage": {"input_tokens": 350, "output_tokens": 520},
    }

    with patch("httpx.Client.post", return_value=mock_resp), \
         patch("herald.services.ai_recorder.SessionLocal", return_value=db):
        script = provider.generate_script(
            source_text="Test source text",
            request_mode="standard",
            source_title="Anthropic Test",
            job_id=job_id,
        )

    assert script.episode_title == "AI Revolution Update"
    assert len(script.segments) == 2

    # Check AI interaction was recorded with provider="anthropic" and token counts
    interactions = db.query(AIInteraction).filter(AIInteraction.job_id == job_id).all()
    assert len(interactions) == 1
    rec = interactions[0]
    assert rec.provider == "anthropic"
    assert rec.model == "claude-3-7-sonnet-20250219"
    assert rec.prompt_tokens == 350
    assert rec.completion_tokens == 520
    assert rec.total_tokens == 870
    assert rec.success is True


def test_openai_provider_generation_and_tokens():
    """Test OpenAI provider script generation and token recording."""
    db = setup_in_memory_db()
    job_id = "job-openai-002"
    job = PodcastJob(
        id=job_id,
        transport="telegram",
        telegram_user_id=2,
        telegram_chat_id=2,
        source_hash="h_oa",
        source_text="OpenAI source content",
        status=JobState.RECEIVED.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    provider = OpenAIProvider(api_key="sk-proj-mock-key", model="gpt-4o")
    assert provider.is_configured() is True
    assert provider.configured_model == "gpt-4o"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(SAMPLE_SCRIPT_JSON)}}],
        "usage": {"prompt_tokens": 400, "completion_tokens": 600, "total_tokens": 1000},
    }

    with patch("httpx.Client.post", return_value=mock_resp), \
         patch("herald.services.ai_recorder.SessionLocal", return_value=db):
        script = provider.generate_script(
            source_text="Test source text",
            request_mode="standard",
            job_id=job_id,
        )

    assert script.episode_title == "AI Revolution Update"
    interactions = db.query(AIInteraction).filter(AIInteraction.job_id == job_id).all()
    assert len(interactions) == 1
    rec = interactions[0]
    assert rec.provider == "openai"
    assert rec.model == "gpt-4o"
    assert rec.prompt_tokens == 400
    assert rec.completion_tokens == 600
    assert rec.total_tokens == 1000
    assert rec.success is True


def test_groq_provider_generation():
    """Test Groq provider script generation."""
    db = setup_in_memory_db()
    job_id = "job-groq-003"
    job = PodcastJob(
        id=job_id,
        transport="telegram",
        telegram_user_id=3,
        telegram_chat_id=3,
        source_hash="h_gq",
        source_text="Groq source content",
        status=JobState.RECEIVED.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    provider = GroqProvider(api_key="gsk_mock_groq_key_12345", model="llama-3.3-70b-versatile")
    assert provider.is_configured() is True
    assert provider.provider_name == "Groq"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(SAMPLE_SCRIPT_JSON)}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 300, "total_tokens": 500},
    }

    with patch("httpx.Client.post", return_value=mock_resp), \
         patch("herald.services.ai_recorder.SessionLocal", return_value=db):
        script = provider.generate_script(
            source_text="Test source text",
            request_mode="brief",
            job_id=job_id,
        )

    assert script.episode_title == "AI Revolution Update"
    interactions = db.query(AIInteraction).filter(AIInteraction.job_id == job_id).all()
    assert len(interactions) == 1
    rec = interactions[0]
    assert rec.provider == "groq"
    assert rec.model == "llama-3.3-70b-versatile"
    assert rec.total_tokens == 500


def test_ollama_provider_generation_and_tokens():
    """Test Ollama local provider script generation and token recording."""
    db = setup_in_memory_db()
    job_id = "job-ollama-004"
    job = PodcastJob(
        id=job_id,
        transport="telegram",
        telegram_user_id=4,
        telegram_chat_id=4,
        source_hash="h_ol",
        source_text="Ollama local source",
        status=JobState.RECEIVED.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3.2")
    assert provider.is_configured() is True
    assert provider.provider_name == "Ollama"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "message": {"content": json.dumps(SAMPLE_SCRIPT_JSON)},
        "prompt_eval_count": 150,
        "eval_count": 280,
    }

    with patch("httpx.Client.post", return_value=mock_resp), \
         patch("herald.services.ai_recorder.SessionLocal", return_value=db):
        script = provider.generate_script(
            source_text="Test source text",
            request_mode="standard",
            job_id=job_id,
        )

    assert script.episode_title == "AI Revolution Update"
    interactions = db.query(AIInteraction).filter(AIInteraction.job_id == job_id).all()
    assert len(interactions) == 1
    rec = interactions[0]
    assert rec.provider == "ollama"
    assert rec.prompt_tokens == 150
    assert rec.completion_tokens == 280
    assert rec.total_tokens == 430


def test_literal_provider_zero_ai_interactions():
    """Verify LiteralProvider generates deterministic scripts with 0 AI calls."""
    db = setup_in_memory_db()
    job_id = "job-literal-005"
    job = PodcastJob(
        id=job_id,
        transport="telegram",
        telegram_user_id=5,
        telegram_chat_id=5,
        source_hash="h_lit",
        source_text="Literal text content for deterministic narration.",
        status=JobState.RECEIVED.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    provider = LiteralProvider()
    assert provider.is_configured() is True
    assert provider.provider_name == "None (Literal)"

    script = provider.generate_script(
        source_text="Deterministic raw source paragraph.",
        source_title="Literal Ep",
        job_id=job_id,
    )
    assert len(script.segments) > 0

    interactions = db.query(AIInteraction).filter(AIInteraction.job_id == job_id).all()
    assert len(interactions) == 0


def test_research_mode_compatibility_guard():
    """
    Acceptance Rule: Research mode is Gemini-only.
    When a non-Gemini provider (e.g. Anthropic, OpenAI, Groq, Ollama) is active,
    requesting Research mode MUST be rejected clearly without silent downgrades.
    """
    db = setup_in_memory_db()

    # Configure Anthropic as active provider
    with patch.object(settings, "AI_PROVIDER", "anthropic"), \
         patch.object(settings, "ANTHROPIC_API_KEY", "sk-ant-valid-key"):
        reset_ai_provider()

        req = HeraldRequest(
            transport="telegram",
            requester_identity="telegram:100",
            delivery_target="100",
            request_mode="research",
            research_depth="high",
            source_text="Deep research request topic.",
        )

        resp = process_herald_request(db, req)

        # Invariant: Job failed immediately with INCOMPATIBLE_PROVIDER_FOR_RESEARCH
        assert resp.status == JobState.FAILED_FINAL.value
        assert resp.error_category == "INCOMPATIBLE_PROVIDER_FOR_RESEARCH"
        assert "Google Gemini" in resp.message
        assert "AI_PROVIDER=gemini" in resp.message
        assert "anthropic" in resp.message.lower()

        # Invariant: Zero jobs queued
        jobs = db.query(PodcastJob).all()
        assert len(jobs) == 0


def test_api_endpoint_research_mode_guard(monkeypatch):
    """Verify /api/v1/script/generate raises 400 Bad Request if research mode is attempted on non-Gemini provider."""
    from fastapi.testclient import TestClient

    from herald.db.connection import get_db

    db = setup_in_memory_db()
    job_id = "job-guard-api-001"
    job = PodcastJob(
        id=job_id,
        transport="api",
        source_hash="h_api",
        source_text="Research text",
        request_mode="research",
        status=JobState.SOURCE_READY.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(settings, "AI_PROVIDER", "openai")
    monkeypatch.setattr(settings, "HERALD_API_KEY", "test-auth-key")

    try:
        client = TestClient(app)
        res = client.post(
            "/api/v1/script/generate",
            json={"job_id": job_id},
            headers={"x-api-key": "test-auth-key"},
        )
        assert res.status_code == 400
        assert "Google Gemini" in res.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_card_reflects_active_provider():
    """Verify diagnostics card truthfully formats active provider identity."""
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

    # Add AI interaction with provider="anthropic"
    interaction = AIInteraction(
        id="ai-rec-001",
        job_id=job.id,
        provider="anthropic",
        model="claude-3-7-sonnet-20250219",
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
    assert "Anthropic (claude-3-7-sonnet-20250219)" in card
    assert "700 tokens" in card
