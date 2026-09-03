import json
from datetime import UTC, datetime

import pytest

from herald.config import settings
from herald.db.models import PodcastJob, RequestMode
from herald.gemini.client import (
    GeminiValidationError,
    generate_grounded_research,
    normalize_research_dossier,
)
from herald.gemini.schema import (
    ResearchDossierResponse,
)


def test_research_model_configuration_difference():
    assert settings.GEMINI_MODEL == "gemini-3.5-flash"
    assert settings.GEMINI_RESEARCH_MODEL == "gemini-2.5-flash"
    assert settings.GEMINI_RESEARCH_MODEL != settings.GEMINI_MODEL


def test_canonical_source_id_registry_creation(monkeypatch):
    source_text = "Primary source detailing quantum coherence testing."

    fake_resp = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Grounded search response text referencing S1 and S2."}]
                },
                "groundingMetadata": {
                    "webSearchQueries": ["quantum coherence testing 2026", "superconducting qubits benchmark"],
                    "groundingChunks": [
                        {"web": {"uri": "https://nature.com/articles/quantum1", "title": "Nature Quantum Benchmark"}},
                        {"web": {"uri": "https://arxiv.org/abs/2608.12345", "title": "arXiv Quantum Paper"}},
                    ],
                },
            }
        ]
    }

    class MockResponse:
        status_code = 200
        def json(self):
            return fake_resp

    class MockClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, json=None, **kwargs):
            return MockResponse()

    monkeypatch.setattr("httpx.Client", MockClient)

    res = generate_grounded_research(source_text, research_depth="medium", api_key="fake-key")

    assert res["search_count"] == 2
    assert res["source_count"] == 2
    sources = res["research_sources"]
    assert len(sources) == 2
    assert sources[0]["source_id"] == "S1"
    assert sources[0]["url"] == "https://nature.com/articles/quantum1"
    assert sources[0]["domain"] == "nature.com"
    assert sources[1]["source_id"] == "S2"
    assert sources[1]["url"] == "https://arxiv.org/abs/2608.12345"


def test_dossier_normalization_rejects_invented_source_ids(monkeypatch):
    source_text = "Primary article text."
    grounded_data = {
        "raw_text": "Grounded evidence text.",
        "research_sources": [
            {
                "source_id": "S1",
                "title": "Grounded Source 1",
                "url": "https://example.com/s1",
                "domain": "example.com",
                "retrieved_at": datetime.now(UTC).isoformat(),
                "search_query": "query 1",
            }
        ],
    }

    # Fake response referencing non-existent source ID 'S99'
    fake_resp = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps({
                                "source_summary": "Summary",
                                "verification": [
                                    {
                                        "source_claim": "Claim 1",
                                        "status": "supported",
                                        "notes": "Verified",
                                        "source_ids": ["S99"],  # Invalid invented ID!
                                    }
                                ],
                                "useful_context": [],
                                "outdated_or_uncertain": [],
                                "research_sources": grounded_data["research_sources"],
                            })
                        }
                    ]
                }
            }
        ]
    }

    class MockResponse:
        status_code = 200
        def json(self):
            return fake_resp

    class MockClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, json=None, **kwargs):
            return MockResponse()

    monkeypatch.setattr("httpx.Client", MockClient)

    with pytest.raises(GeminiValidationError) as excinfo:
        normalize_research_dossier(source_text, grounded_data, api_key="fake-key")

    assert "invalid source ID 'S99'" in str(excinfo.value)


def test_research_artifacts_generation(tmp_path):
    job = PodcastJob(
        id="job-res-001",
        gmail_message_id="msg-res-1",
        sender_email="user@example.com",
        request_mode=RequestMode.RESEARCH.value,
        research_depth="high",
        custom_title="Quantum Physics Upgrade",
        created_at=datetime.now(UTC),
        script_json={
            "episode_title": "Quantum Physics Upgrade",
            "episode_description": "Comprehensive episode on quantum benchmarks.",
            "segments": [{"order": 1, "heading": "Intro", "narration": "Narration text"}],
            "warnings": [],
        },
        research_json={
            "source_summary": "Primary source summary...",
            "verification": [
                {
                    "source_claim": "Coherence time reached 5ms",
                    "status": "supported",
                    "notes": "Confirmed by Nature paper.",
                    "source_ids": ["S1"],
                }
            ],
            "useful_context": [
                {
                    "fact": "Qubit fidelity was 99.9%",
                    "why_it_matters": "Meets error correction threshold.",
                    "source_ids": ["S2"],
                }
            ],
            "outdated_or_uncertain": ["Prior 2024 figure of 1ms is now outdated."],
            "research_sources": [
                {
                    "source_id": "S1",
                    "title": "Nature Benchmark",
                    "url": "https://nature.com/articles/q1",
                    "domain": "nature.com",
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "search_query": "quantum benchmark",
                },
                {
                    "source_id": "S2",
                    "title": "IEEE Qubit Study",
                    "url": "https://ieee.org/qubit",
                    "domain": "ieee.org",
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "search_query": "qubit fidelity",
                },
            ],
        },
        research_model="gemini-2.5-flash",
        research_search_count=3,
        research_source_count=2,
        research_repair_count=0,
    )

    from herald.audio.artifact_generator import ensure_details_artifact
    p_details = ensure_details_artifact(job, tmp_path)

    assert p_details.exists()
    assert p_details.name.endswith("_details.md")

    md_content = p_details.read_text(encoding="utf-8")
    assert "# Herald Episode Details" in md_content
    assert "Coherence time reached 5ms" in md_content
    assert "Nature Benchmark" in md_content
    assert "https://nature.com/articles/q1" in md_content


def test_generate_script_endpoint_research_mode_logging_and_pipeline(monkeypatch, db_session):
    """
    Integration test exercising POST /api/v1/script/generate endpoint for Research mode.
    Verifies that logging calls (logger.info) in Stage 1a, 1b, 2, 3 execute cleanly
    without NameError or unhandled runtime exceptions.
    """
    from fastapi.testclient import TestClient

    from apps.api.main import app
    from herald.db.models import JobState, PodcastJob
    from herald.gemini.schema import PodcastScriptResponse, ResearchAuditResponse

    monkeypatch.setattr(settings, "HERALD_ENV", "testing")

    job_id = "job-api-research-test-001"
    job = PodcastJob(
        id=job_id,
        gmail_message_id="msg-api-res-1",
        sender_email="auth@example.com",
        request_mode=RequestMode.RESEARCH.value,
        research_depth="high",
        source_type="email_body",
        source_hash="hash-api-res-1",
        source_text="Primary article source material for quantum research.",
        custom_title="Quantum Endpoint Test",
        status=JobState.SOURCE_READY.value,
    )
    db_session.add(job)
    db_session.commit()

    # Mock Gemini Research calls
    def mock_grounded_research(source_text, research_depth="medium", *args, **kwargs):
        return {
            "raw_text": "Grounded evidence text",
            "search_count": 2,
            "source_count": 1,
            "research_sources": [
                {
                    "source_id": "S1",
                    "title": "Grounded Source",
                    "url": "https://example.com/s1",
                    "domain": "example.com",
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "search_query": "quantum research",
                }
            ],
        }

    def mock_normalize_dossier(source_text, grounded_research_data, *args, **kwargs):
        return ResearchDossierResponse(
            source_summary="Summary",
            verification=[
                {
                    "source_claim": "Claim",
                    "status": "supported",
                    "notes": "Verified",
                    "source_ids": ["S1"],
                }
            ],
            useful_context=[],
            outdated_or_uncertain=[],
            research_sources=grounded_research_data["research_sources"],
        )

    def mock_generate_script(source_text, request_mode, research_dossier=None, source_title=None, *args, **kwargs):
        return PodcastScriptResponse(
            episode_title="Quantum Endpoint Test",
            episode_description="Description",
            estimated_minutes=3,
            segments=[
                {"order": 1, "heading": "Intro", "narration": "Welcome to the podcast narration."}
            ],
            warnings=[],
        )

    def mock_audit_script(source_text, research_dossier, script_dict, *args, **kwargs):
        return ResearchAuditResponse(has_material_issues=False)

    monkeypatch.setattr("apps.api.main.generate_grounded_research", mock_grounded_research)
    monkeypatch.setattr("apps.api.main.normalize_research_dossier", mock_normalize_dossier)
    monkeypatch.setattr("apps.api.main.generate_podcast_script", mock_generate_script)
    monkeypatch.setattr("apps.api.main.audit_research_script", mock_audit_script)

    client = TestClient(app)
    res = client.post("/api/v1/script/generate", json={"job_id": job_id})

    assert res.status_code == 200, f"Expected 200 OK, got {res.status_code}: {res.text}"
    data = res.json()
    assert data["job_id"] == job_id
    assert data["status"] == JobState.QUEUED_TTS.value
    assert data["request_mode"] == "research"
    assert data["research_depth"] == "high"

    # Verify database persistence across all pipeline stages
    updated_job = db_session.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    assert updated_job.research_grounding_json is not None
    assert updated_job.research_json is not None
    assert updated_job.script_json is not None
    assert updated_job.research_audit_json is not None

