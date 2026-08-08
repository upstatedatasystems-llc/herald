import json
import pytest
from datetime import UTC, datetime

from herald.audio.artifact_generator import (
    ensure_research_artifact,
    ensure_research_notes_artifact,
    ensure_script_artifact,
    get_artifact_filenames,
)
from herald.config import settings
from herald.db.models import PodcastJob, RequestMode
from herald.gemini.client import (
    GeminiValidationError,
    generate_grounded_research,
    normalize_research_dossier,
)
from herald.gemini.schema import (
    ResearchDossierResponse,
    ResearchSource,
    UsefulContextItem,
    VerificationItem,
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
        def post(self, url, json):
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
        def post(self, url, json):
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

    p_script = ensure_script_artifact(job, tmp_path)
    p_json = ensure_research_artifact(job, tmp_path)
    p_md = ensure_research_notes_artifact(job, tmp_path)

    assert p_script.exists()
    assert p_json.exists()
    assert p_md.exists()

    md_content = p_md.read_text(encoding="utf-8")
    assert "# Research Notes: Quantum Physics Upgrade" in md_content
    assert "High" in md_content
    assert "Coherence time reached 5ms" in md_content
    assert "Nature Benchmark" in md_content
    assert "https://nature.com/articles/q1" in md_content
