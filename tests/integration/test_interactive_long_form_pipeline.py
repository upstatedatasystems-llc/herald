"""
Integration test suite for Herald's interactive long-form pipeline and branding architecture.

Validates complete vertical slices across all four content modes:
- Test A: Topic / fixed duration (20m) / grounded research
- Test B: Expanded / fixed duration (10m) / source + grounded research
- Test C: Source / fixed duration (10m) / source-only (zero research)
- Test D: Literal / verbatim (zero AI scripting)
"""

import os
from unittest.mock import MagicMock
from pathlib import Path
import pytest

from apps.worker import main as worker_main
from herald.ai.base import AIProvider
from herald.ai.gemini_provider import GeminiProvider
from herald.ai.literal_provider import LiteralProvider
from herald.ai.schema import PodcastScriptResponse, PodcastSegment
from herald.audio.branding import render_intro_narration, render_outro_narration
from herald.config import settings
from herald.core.pipeline import execute_script_generation
from herald.db.models import JobState, PodcastJob
from herald.tts.kokoro_client import KokoroClient


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    """Setup common test environment settings for long-form pipeline testing."""
    monkeypatch.setenv("HERALD_MOCK_TTS", "1")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(settings, "GEMINI_RESEARCH_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(settings, "BRANDING_INTRO_ENABLED", True)
    monkeypatch.setattr(settings, "BRANDING_OUTRO_ENABLED", True)
    monkeypatch.setattr(settings, "BRANDING_PLATFORM_NAME", "Herald")

    # Mock join_and_normalize_audio to avoid requiring ffmpeg binary in tests
    def mock_join_and_normalize_audio(**kwargs):
        out_path = Path(kwargs["output_mp3_path"])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\xFF\xFB\x90\x44" * 100)
        return {
            "output_path": str(out_path),
            "file_bytes": 400,
            "duration_seconds": 60.0,
            "sha256": "fake-sha-256",
        }

    monkeypatch.setattr("apps.worker.main.join_and_normalize_audio", mock_join_and_normalize_audio)


def test_vertical_slice_topic_mode_20m_research(db_session, monkeypatch):
    """
    Test A: Topic / 20 min / Research
    - Scope: EvidenceScope.RESEARCH
    - Triggers grounded research
    - Real GeminiProvider adapter invoked
    - Telemetry snapshots research_provider and research_model
    - Worker claims SCRIPTING and QUEUED_TTS
    - Intro narration announces ~20 min topic podcast
    - Transitions to AUDIO_READY
    """
    import herald.gemini.client as gem_client

    researched_topics = []

    def mock_grounded_research(source_text, research_depth="medium", model_name=None, job_id=None, research_plan=None, api_key=None, **kwargs):
        researched_topics.append(source_text)
        return {
            "raw_text": "Deep research notes on Fusion Energy.",
            "search_count": 3,
            "source_count": 3,
            "research_sources": [
                {"title": "Fusion Breakthrough", "snippet": "Net energy achieved at facility.", "url": "https://fusion.org/1"},
                {"title": "Magnetic Confinement", "snippet": "Tokamak designs reach record temperatures.", "url": "https://fusion.org/2"},
                {"title": "Commercial Roadmaps", "snippet": "Grid deployment targeted for next decade.", "url": "https://fusion.org/3"},
            ],
            "grounding_supports": [],
        }

    monkeypatch.setattr(gem_client, "generate_grounded_research", mock_grounded_research)

    # Mock Gemini script generation to produce realistic long-form sections
    def mock_generate_script(*args, **kwargs):
        return PodcastScriptResponse(
            episode_title="Fusion Energy: The Next Frontier",
            episode_description="A 20-minute exploration of magnetic confinement fusion.",
            segments=[
                PodcastSegment(order=1, heading="Introduction to Fusion", narration="Welcome. Fusion energy represents the ultimate power source. " * 30),
                PodcastSegment(order=2, heading="Physics and Containment", narration="Inside tokamaks, magnetic fields bottle up high-temperature plasma. " * 30),
                PodcastSegment(order=3, heading="Commercial Outlook", narration="Multiple ventures are now racing to deliver grid-scale energy. " * 30),
            ],
            warnings=[],
        )

    real_gemini = GeminiProvider(model="gemini-2.5-flash", research_model="gemini-2.5-flash")
    monkeypatch.setattr(real_gemini, "generate_script", mock_generate_script)
    monkeypatch.setattr("herald.ai.failover.create_provider", lambda p, **kw: real_gemini if p == "gemini" else LiteralProvider())
    monkeypatch.setattr("herald.ai.registry.create_provider", lambda p, **kw: real_gemini if p == "gemini" else LiteralProvider())

    # Step 1: Create Job in SCRIPTING state
    job = PodcastJob(
        id="job-topic-20m",
        source_hash="hash-topic-20m",
        source_text="Nuclear Fusion Commercialization",
        custom_title="Nuclear Fusion Commercialization",
        content_mode="topic",
        request_mode="research",
        target_minutes="20",
        research_depth="medium",
        status=JobState.SCRIPTING.value,
        ai_provider_chain_json=[{"provider": "gemini", "model": "gemini-2.5-flash"}],
    )
    db_session.add(job)
    db_session.commit()

    # Step 2: Worker processes SCRIPTING
    processed_script = worker_main.process_next_scripting_job(db_session, worker_id="worker-integration")
    assert processed_script is True

    db_session.refresh(job)
    assert job.status == JobState.QUEUED_TTS.value
    assert job.research_provider == "gemini"
    assert job.research_model == "gemini-2.5-flash"
    assert len(researched_topics) > 0
    assert job.script_json is not None
    assert len(job.script_json["segments"]) >= 3

    # Step 3: Verify Branding Intro Announcement
    intro_text = render_intro_narration(
        topic=job.custom_title,
        target_minutes=job.target_minutes,
        actual_body_duration_seconds=1200.0,
        content_mode=job.content_mode,
    )
    assert "20-minute" in intro_text
    assert "Nuclear Fusion Commercialization" in intro_text

    # Step 4: Worker processes TTS & Branding
    kokoro = KokoroClient()
    processed_tts = worker_main.process_next_job(db_session, kokoro, worker_id="worker-integration")
    assert processed_tts is True

    db_session.refresh(job)
    assert job.status == JobState.AUDIO_READY.value
    assert job.local_audio_path is not None
    assert job.branding_intro_seconds > 0
    assert job.branding_outro_seconds > 0


def test_vertical_slice_expanded_mode_10m_research(db_session, monkeypatch):
    """
    Test B: Expanded / 10 min / Research
    - Scope: EvidenceScope.SOURCE_PLUS_RESEARCH
    - Combines source text + grounded research
    - Fidelity audits check both seed and research
    - Transitions to AUDIO_READY
    """
    import herald.gemini.client as gem_client

    def mock_grounded_research(source_text, research_depth="medium", model_name=None, job_id=None, research_plan=None, api_key=None, **kwargs):
        return {
            "raw_text": "Expanded context on battery chemistry.",
            "search_count": 2,
            "source_count": 2,
            "research_sources": [
                {"title": "Solid State Batteries", "snippet": "Solid electrolyte reduces dendrite formation.", "url": "https://battery.org/1"},
                {"title": "Manufacturing Scaling", "snippet": "Roll-to-roll processes show promise.", "url": "https://battery.org/2"},
            ],
            "grounding_supports": [],
        }

    monkeypatch.setattr(gem_client, "generate_grounded_research", mock_grounded_research)

    def mock_generate_script(*args, **kwargs):
        return PodcastScriptResponse(
            episode_title="The Solid-State Revolution",
            episode_description="A 10-minute deep dive into battery chemistry.",
            segments=[
                PodcastSegment(order=1, heading="Introduction", narration="Lithium metal batteries are transforming energy storage. " * 25),
                PodcastSegment(order=2, heading="Electrolyte Innovation", narration="Replacing liquid flammable electrolytes with ceramic separators is key. " * 25),
                PodcastSegment(order=3, heading="Market Horizon", narration="Automakers anticipate commercial vehicles by the end of the decade. " * 25),
            ],
            warnings=[],
        )

    real_gemini = GeminiProvider(model="gemini-2.5-flash", research_model="gemini-2.5-flash")
    monkeypatch.setattr(real_gemini, "generate_script", mock_generate_script)
    monkeypatch.setattr("herald.ai.failover.create_provider", lambda p, **kw: real_gemini if p == "gemini" else LiteralProvider())
    monkeypatch.setattr("herald.ai.registry.create_provider", lambda p, **kw: real_gemini if p == "gemini" else LiteralProvider())

    seed_text = (
        "QuantumScape and partner manufacturers have demonstrated initial test cycles of solid-state separator cells. "
        "The proprietary ceramic separator eliminates the need for an anode during manufacturing, significantly boosting volumetric energy density."
    )

    job = PodcastJob(
        id="job-expanded-10m",
        source_hash="hash-expanded-10m",
        source_text=seed_text,
        custom_title="Solid-State Battery Progress",
        content_mode="expanded",
        request_mode="research",
        target_minutes="10",
        research_depth="medium",
        status=JobState.SCRIPTING.value,
        ai_provider_chain_json=[{"provider": "gemini", "model": "gemini-2.5-flash"}],
    )
    db_session.add(job)
    db_session.commit()

    # Worker SCRIPTING
    processed_script = worker_main.process_next_scripting_job(db_session, worker_id="worker-integration")
    assert processed_script is True

    db_session.refresh(job)
    assert job.status == JobState.QUEUED_TTS.value
    assert job.research_provider == "gemini"
    assert job.research_model == "gemini-2.5-flash"
    assert job.fidelity_audit_json is not None
    assert "status" in job.fidelity_audit_json

    # Worker TTS & Branding
    kokoro = KokoroClient()
    processed_tts = worker_main.process_next_job(db_session, kokoro, worker_id="worker-integration")
    assert processed_tts is True

    db_session.refresh(job)
    assert job.status == JobState.AUDIO_READY.value
    assert job.branding_intro_seconds > 0


def test_vertical_slice_source_mode_10m_source_only(db_session, monkeypatch):
    """
    Test C: Source / 10 min / Source-only
    - Scope: EvidenceScope.SOURCE_ONLY
    - Zero grounded research calls
    - Faithful coverage ledger extracted from source
    - Transitions to AUDIO_READY
    """
    import herald.gemini.client as gem_client

    research_called = False

    def mock_grounded_research(*args, **kwargs):
        nonlocal research_called
        research_called = True
        return {}

    monkeypatch.setattr(gem_client, "generate_grounded_research", mock_grounded_research)

    def mock_generate_script(*args, **kwargs):
        return PodcastScriptResponse(
            episode_title="Internal Architecture Report",
            episode_description="A faithful walkthrough of the technical report.",
            segments=[
                PodcastSegment(order=1, heading="Section 1", narration="The telemetry architecture streams real-time metrics across workers. " * 30),
                PodcastSegment(order=2, heading="Section 2", narration="Database locks guarantee idempotent task acquisition. " * 30),
            ],
            warnings=[],
        )

    real_gemini = GeminiProvider(model="gemini-2.5-flash", research_model="gemini-2.5-flash")
    monkeypatch.setattr(real_gemini, "generate_script", mock_generate_script)
    monkeypatch.setattr("herald.ai.failover.create_provider", lambda p, **kw: real_gemini if p == "gemini" else LiteralProvider())
    monkeypatch.setattr("herald.ai.registry.create_provider", lambda p, **kw: real_gemini if p == "gemini" else LiteralProvider())

    source_text = (
        "Internal Architecture Report.\n\n"
        "1. Distributed worker nodes use database-backed leases with a five-minute TTL.\n"
        "2. Heartbeats renew active leases every 30 seconds during intense synthesis stages.\n"
        "3. Any expired lease is cleanly recovered without data corruption."
    )

    job = PodcastJob(
        id="job-source-10m",
        source_hash="hash-source-10m",
        source_text=source_text,
        custom_title="Internal Architecture Report",
        content_mode="source",
        request_mode="standard",
        target_minutes="10",
        research_depth=None,
        status=JobState.SCRIPTING.value,
        ai_provider_chain_json=[{"provider": "gemini", "model": "gemini-2.5-flash"}],
    )
    db_session.add(job)
    db_session.commit()

    # Worker SCRIPTING
    processed_script = worker_main.process_next_scripting_job(db_session, worker_id="worker-integration")
    assert processed_script is True
    assert not research_called, "Source mode must NOT trigger grounded research"

    db_session.refresh(job)
    assert job.status == JobState.QUEUED_TTS.value
    assert job.research_provider is None
    assert job.research_model is None

    # Worker TTS & Branding
    kokoro = KokoroClient()
    processed_tts = worker_main.process_next_job(db_session, kokoro, worker_id="worker-integration")
    assert processed_tts is True

    db_session.refresh(job)
    assert job.status == JobState.AUDIO_READY.value


def test_vertical_slice_literal_mode_zero_ai(db_session, monkeypatch):
    """
    Test D: Literal / Zero AI
    - Bypasses AI scripting completely
    - Locks target_minutes to 'auto'
    - Creates single verbatim segment
    - Intro branding uses general template (no duration announced)
    - Transitions directly from QUEUED_TTS to AUDIO_READY
    """
    verbatim_text = (
        "To be, or not to be, that is the question: "
        "Whether 'tis nobler in the mind to suffer "
        "The slings and arrows of outrageous fortune, "
        "Or to take arms against a sea of troubles."
    )

    # In Literal mode, intake directly prepares the script without LLM and queues for TTS
    from herald.literal.script_generator import generate_literal_script

    lit_script = generate_literal_script(verbatim_text, source_title="Hamlet Soliloquy").model_dump()

    job = PodcastJob(
        id="job-literal-0ai",
        source_hash="hash-literal-0ai",
        source_text=verbatim_text,
        custom_title="Hamlet Soliloquy",
        content_mode="literal",
        request_mode="literal",
        target_minutes="auto",
        research_depth=None,
        script_json=lit_script,
        status=JobState.QUEUED_TTS.value,
    )
    db_session.add(job)
    db_session.commit()

    # Verify Branding intro uses general template without duration
    intro_narration = render_intro_narration(
        topic=job.custom_title,
        target_minutes=job.target_minutes,
        actual_body_duration_seconds=30.0,
        content_mode=job.content_mode,
    )
    assert "Hamlet Soliloquy" in intro_narration
    assert "minute" not in intro_narration  # Suppresses duration in literal mode

    # Worker processes TTS directly
    kokoro = KokoroClient()
    processed_tts = worker_main.process_next_job(db_session, kokoro, worker_id="worker-integration")
    assert processed_tts is True

    db_session.refresh(job)
    assert job.status == JobState.AUDIO_READY.value
    assert job.branding_intro_seconds > 0
    assert job.branding_outro_seconds > 0
