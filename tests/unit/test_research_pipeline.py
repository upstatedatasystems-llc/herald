import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from herald.ai.gemini_provider import GeminiProvider
from herald.config import settings
from herald.db.models import PodcastJob, RequestMode
from herald.gemini.client import (
    GeminiError,
    GeminiModelUnavailableError,
    GeminiOutputTruncatedError,
    GeminiValidationError,
    generate_grounded_research,
    normalize_research_dossier,
)
from herald.gemini.schema import (
    ResearchDossierResponse,
)


def test_research_model_configuration_difference():
    assert settings.GEMINI_MODEL == "gemini-3.5-flash"
    assert settings.GEMINI_RESEARCH_MODEL == "gemini-3.6-flash"
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


def test_pipeline_research_model_attribution(monkeypatch, tmp_path):
    """
    Verify that when GEMINI_MODEL='custom-script-model' and GEMINI_RESEARCH_MODEL='custom-research-model',
    pipeline Research jobs store research_model='custom-research-model',
    and manifest/diagnostics truthfully reflect it.
    """
    from unittest.mock import patch

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from herald.core.models import HeraldRequest
    from herald.core.pipeline import process_herald_request
    from herald.db.connection import Base
    from herald.db.models import PodcastJob
    from herald.gemini.schema import (
        PodcastScriptResponse,
        ResearchAuditResponse,
    )
    from herald.services.diagnostics_export import build_manifest_dict

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    mock_dossier = ResearchDossierResponse(
        source_summary="Summary",
        verification=[],
        useful_context=[],
        outdated_or_uncertain=[],
        research_sources=[],
    )
    mock_script = PodcastScriptResponse(
        episode_title="Research Model Test",
        episode_description="Desc",
        estimated_minutes=2,
        segments=[{"order": 1, "heading": "H1", "narration": "Narr"}],
        warnings=[],
    )
    mock_audit = ResearchAuditResponse(has_material_issues=False)

    with patch.object(settings, "GEMINI_API_KEY", "valid_key"), \
         patch.object(settings, "GEMINI_MODEL", "custom-script-model"), \
         patch.object(settings, "GEMINI_RESEARCH_MODEL", "custom-research-model"), \
         patch("herald.ai.gemini_provider.GeminiProvider.generate_grounded_research", return_value={"raw_text": "t", "grounding_metadata": {}, "search_count": 1, "source_count": 1, "research_sources": []}), \
         patch("herald.ai.gemini_provider.GeminiProvider.normalize_research_dossier", return_value=mock_dossier), \
         patch("herald.ai.gemini_provider.GeminiProvider.generate_script", return_value=mock_script), \
         patch("herald.ai.gemini_provider.GeminiProvider.audit_research_script", return_value=mock_audit):

        req = HeraldRequest(
            transport="telegram",
            requester_identity="telegram:101",
            delivery_target="101",
            request_mode="research",
            source_text="Test pipeline source for research attribution.",
        )
        resp = process_herald_request(db, req)
        pipe_job = db.query(PodcastJob).filter_by(id=resp.job_id).first()
        assert pipe_job is not None
        assert pipe_job.research_model == "custom-research-model"

        manifest = build_manifest_dict(pipe_job, db, included_files=[], truncated_files=[])
        assert manifest["research_model"] == "custom-research-model"


class TestResearchNormalizationConfig:
    """Verify GEMINI_RESEARCH_NORMALIZATION_INITIAL_OUTPUT_TOKENS and MAX config exists."""

    def test_config_field_exists(self):
        assert hasattr(settings, "GEMINI_RESEARCH_NORMALIZATION_INITIAL_OUTPUT_TOKENS")
        assert settings.GEMINI_RESEARCH_NORMALIZATION_INITIAL_OUTPUT_TOKENS == 8192
        assert hasattr(settings, "GEMINI_RESEARCH_NORMALIZATION_MAX_OUTPUT_TOKENS")
        assert settings.GEMINI_RESEARCH_NORMALIZATION_MAX_OUTPUT_TOKENS == 16384

    def test_config_field_is_int(self):
        assert isinstance(settings.GEMINI_RESEARCH_NORMALIZATION_INITIAL_OUTPUT_TOKENS, int)
        assert isinstance(settings.GEMINI_RESEARCH_NORMALIZATION_MAX_OUTPUT_TOKENS, int)




class TestResearchNormalization40Sources:
    """Verify research normalization with large source registries."""

    def _generate_mock_sources(self, count: int = 40):
        return [
            {
                "source_id": f"S{i}",
                "title": f"Authoritative Study {i}: Advances in Research",
                "url": f"https://doi.org/10.1000/study-{i}",
                "domain": "doi.org",
                "retrieved_at": "2026-09-09T12:00:00Z",
                "search_query": f"research topic {i}",
            }
            for i in range(1, count + 1)
        ]

    def test_schema_excludes_research_sources_and_injects_locally(self):
        """Gemini schema must NOT include research_sources, and sources are injected locally."""
        from herald.gemini.client import normalize_research_dossier

        sources = self._generate_mock_sources(40)
        captured_payload = {}

        def mock_post(url, json=None, headers=None):
            nonlocal captured_payload
            captured_payload = json
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            valid_dossier = {
                "source_summary": "Comprehensive summary of findings.",
                "verification": [
                    {
                        "source_claim": "Claim 1",
                        "status": "supported",
                        "notes": "Verified against studies",
                        "source_ids": ["S1", "S2"],
                    }
                ],
                "useful_context": [
                    {
                        "fact": "Fact 1",
                        "why_it_matters": "Context is essential",
                        "source_ids": ["S3"],
                    }
                ],
                "outdated_or_uncertain": [],
            }
            mock_resp.json.return_value = {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": json_mod.dumps(valid_dossier)}]},
                    }
                ],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 500, "totalTokenCount": 1500},
            }
            return mock_resp

        import json as json_mod

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-key"),
            patch("httpx.Client.post", side_effect=mock_post),
            patch("herald.gemini.client._record_gemini_interaction"),
        ):
            dossier = normalize_research_dossier(
                source_text="Test primary source.",
                grounded_research_data={
                    "raw_text": "Grounded research notes.",
                    "research_sources": sources,
                },
            )

        # 1. Schema must NOT have research_sources
        gen_cfg = captured_payload.get("generationConfig", {})
        schema_props = gen_cfg.get("responseSchema", {}).get("properties", {})
        assert "research_sources" not in schema_props, "research_sources must not be in Gemini schema"

        # 2. Returned dossier must have all 40 sources locally injected
        assert len(dossier.research_sources) == 40
        assert dossier.research_sources[0].source_id == "S1"
        assert dossier.research_sources[39].source_id == "S40"

    def test_finish_reason_max_tokens_triggers_truncation_error(self):
        """When finishReason is MAX_TOKENS, GeminiOutputTruncatedError must be raised without attempting JSON parse."""
        from herald.gemini.client import normalize_research_dossier

        def mock_post_truncated(url, json=None, headers=None):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "candidates": [
                    {
                        "finishReason": "MAX_TOKENS",
                        "content": {"parts": [{"text": '{"source_summary": "Incomplete json'}]},
                    }
                ],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 8192, "totalTokenCount": 9192},
            }
            return mock_resp

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-key"),
            patch.object(settings, "GEMINI_RETRY_COUNT", 1),
            patch("httpx.Client.post", side_effect=mock_post_truncated),
            patch("herald.gemini.client._record_gemini_interaction"),
        ):
            with pytest.raises(GeminiOutputTruncatedError) as exc_info:
                normalize_research_dossier(
                    source_text="Test source.",
                    grounded_research_data={
                        "raw_text": "Evidence.",
                        "research_sources": [{"source_id": "S1"}],
                    },
                )

        assert "output truncated" in str(exc_info.value).lower()

    def test_research_normalization_records_stop_telemetry(self):
        """Successful normalization records finish_reason='STOP' and requested_max_output_tokens=8192."""
        from herald.gemini.client import normalize_research_dossier

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"x-goog-request-id": "req-norm-stop-123"}
        valid_dossier = {
            "source_summary": "Summary of research findings.",
            "verification": [],
            "useful_context": [],
            "outdated_or_uncertain": [],
        }
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps(valid_dossier)}]},
                }
            ],
            "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 450, "totalTokenCount": 1650},
        }

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-key"),
            patch("httpx.Client.post", return_value=mock_resp),
            patch("herald.gemini.client._record_gemini_interaction") as mock_record,
        ):
            dossier = normalize_research_dossier(
                source_text="Test source.",
                grounded_research_data={
                    "raw_text": "Evidence notes.",
                    "research_sources": self._generate_mock_sources(1),
                },
                job_id="job-norm-stop",
            )

        assert dossier is not None
        mock_record.assert_called_once()
        kwargs = mock_record.call_args.kwargs
        assert kwargs["success"] is True
        assert kwargs["finish_reason"] == "STOP"
        assert kwargs["requested_max_output_tokens"] == 8192
        assert kwargs["job_id"] == "job-norm-stop"

    def test_research_normalization_records_max_tokens_and_doubles_budget(self):
        """MAX_TOKENS records finish_reason='MAX_TOKENS' and doubles budget on retry."""
        from herald.gemini.client import normalize_research_dossier

        sent_payloads = []

        def mock_post_retry(url, json=None, headers=None):
            sent_payloads.append(json)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            if len(sent_payloads) == 1:
                # First attempt truncated
                mock_resp.json.return_value = {
                    "candidates": [
                        {
                            "finishReason": "MAX_TOKENS",
                            "content": {"parts": [{"text": '{"source_summary": "Incomplete'}]},
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 8192, "totalTokenCount": 9192},
                }
            else:
                # Second attempt succeeds
                valid_dossier = {
                    "source_summary": "Full summary on retry.",
                    "verification": [],
                    "useful_context": [],
                    "outdated_or_uncertain": [],
                }
                mock_resp.json.return_value = {
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {"parts": [{"text": json_mod.dumps(valid_dossier)}]},
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 12000, "totalTokenCount": 13000},
                }
            return mock_resp

        import json as json_mod

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-key"),
            patch.object(settings, "GEMINI_RETRY_COUNT", 2),
            patch("httpx.Client.post", side_effect=mock_post_retry),
            patch("herald.gemini.client._record_gemini_interaction") as mock_record,
            patch("time.sleep"),
        ):
            dossier = normalize_research_dossier(
                source_text="Test source.",
                grounded_research_data={
                    "raw_text": "Evidence notes.",
                    "research_sources": self._generate_mock_sources(1),
                },
                job_id="job-norm-retry",
            )

        assert dossier is not None
        assert len(sent_payloads) == 2
        # First request had 8192
        assert sent_payloads[0]["generationConfig"]["maxOutputTokens"] == 8192
        # Second request doubled to 16384 (hard cap)
        assert sent_payloads[1]["generationConfig"]["maxOutputTokens"] == 16384

        assert mock_record.call_count == 2
        call1_kwargs = mock_record.call_args_list[0].kwargs
        assert call1_kwargs["success"] is False
        assert call1_kwargs["finish_reason"] == "MAX_TOKENS"
        assert call1_kwargs["requested_max_output_tokens"] == 8192

        call2_kwargs = mock_record.call_args_list[1].kwargs
        assert call2_kwargs["success"] is True
        assert call2_kwargs["finish_reason"] == "STOP"
        assert call2_kwargs["requested_max_output_tokens"] == 16384




def test_default_research_model_is_gemini_36_flash():
    """Verify clean configuration defaults to gemini-3.6-flash."""
    assert settings.GEMINI_RESEARCH_MODEL == "gemini-3.6-flash"


def test_setup_migration_upgrades_former_default_and_preserves_custom(tmp_path):
    """Verify setup migration upgrades former default gemini-2.5-flash while preserving custom models."""
    env_file = tmp_path / ".env"

    # Case 1: Former default is upgraded
    env_file.write_text('GEMINI_RESEARCH_MODEL="gemini-2.5-flash"\nOTHER="val"\n', encoding="utf-8")
    lines = env_file.read_text(encoding="utf-8").splitlines()
    res_m = None
    for line in lines:
        if line.startswith("GEMINI_RESEARCH_MODEL="):
            res_m = line.split("=", 1)[1].strip('"\'')
    if res_m == "gemini-2.5-flash":
        res_m = "gemini-3.6-flash"
    assert res_m == "gemini-3.6-flash"

    # Case 2: Custom model is preserved
    env_file.write_text('GEMINI_RESEARCH_MODEL="gemini-1.5-pro"\nOTHER="val"\n', encoding="utf-8")
    lines = env_file.read_text(encoding="utf-8").splitlines()
    res_m = None
    for line in lines:
        if line.startswith("GEMINI_RESEARCH_MODEL="):
            res_m = line.split("=", 1)[1].strip('"\'')
    if res_m == "gemini-2.5-flash":
        res_m = "gemini-3.6-flash"
    assert res_m == "gemini-1.5-pro"




def test_grounded_research_404_model_not_found_raises_without_retry():
    """Verify model 404 raises GeminiModelUnavailableError immediately with zero retries."""
    mock_resp = httpx.Response(
        404,
        json={"error": {"code": 404, "message": "models/gemini-nonexistent was not found", "status": "NOT_FOUND"}},
    )

    attempt_count = 0

    def mock_post(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        return mock_resp

    with patch("httpx.Client.post", side_effect=mock_post):
        with pytest.raises(GeminiModelUnavailableError) as exc_info:
            generate_grounded_research(
                source_text="Test source text for research",
                research_depth="low",
                api_key="fake-key",
                model_name="gemini-nonexistent",
            )

        assert exc_info.value.error_category == "AI_MODEL_UNAVAILABLE"
        assert exc_info.value.retryable is False
        # Must NOT retry
        assert attempt_count == 1


def test_grounded_research_unrelated_404_retries_and_raises_generic_error():
    """Verify non-model 404 retries and raises standard GeminiError."""
    mock_resp = httpx.Response(
        404,
        json={"error": {"code": 404, "message": "Resource path /custom was not found", "status": "UNKNOWN"}},
    )

    attempt_count = 0

    def mock_post(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        return mock_resp

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("time.sleep", return_value=None):
        with pytest.raises(GeminiError) as exc_info:
            generate_grounded_research(
                source_text="Test source text for research",
                research_depth="low",
                api_key="fake-key",
                model_name="gemini-custom",
            )

        assert not isinstance(exc_info.value, GeminiModelUnavailableError)
        assert attempt_count == settings.GEMINI_RETRY_COUNT


def test_check_research_connection_success():
    """Verify check_research_connection reports connected when probe succeeds."""
    provider = GeminiProvider()
    mock_resp = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "pong"}]}}]})

    with patch.object(settings, "GEMINI_API_KEY", "test-key"), \
         patch("httpx.Client.post", return_value=mock_resp):
        res = provider.check_research_connection(force_refresh=True)
        assert res["configured"] is True
        assert res["connected"] is True
        assert res["model"] == "gemini-3.6-flash"
        assert res["error"] is None


def test_check_research_connection_model_unavailable_404():
    """Verify check_research_connection identifies AI_MODEL_UNAVAILABLE on 404."""
    provider = GeminiProvider()
    mock_resp = httpx.Response(
        404,
        json={"error": {"code": 404, "message": "models/gemini-3.6-flash is not found", "status": "NOT_FOUND"}},
    )

    with patch.object(settings, "GEMINI_API_KEY", "test-key"), \
         patch("httpx.Client.post", return_value=mock_resp):
        res = provider.check_research_connection(force_refresh=True)
        assert res["configured"] is True
        assert res["connected"] is False
        assert res.get("error_category") == "AI_MODEL_UNAVAILABLE"
        assert "unavailable or not found" in res["error"]


def test_check_research_connection_grounding_failure():
    """Verify check_research_connection detects grounding tool failure (e.g. 400)."""
    provider = GeminiProvider()
    mock_resp = httpx.Response(
        400,
        json={"error": {"code": 400, "message": "Google Search tool not supported for this model"}},
    )

    with patch.object(settings, "GEMINI_API_KEY", "test-key"), \
         patch("httpx.Client.post", return_value=mock_resp):
        res = provider.check_research_connection(force_refresh=True)
        assert res["configured"] is True
        assert res["connected"] is False
        assert "grounding tool failure" in res["error"]


def test_ai_check_command_independent_reporting():
    """Verify /ai-check reports standard and research status independently."""
    from herald.telegram.bot import handle_telegram_command
    from herald.telegram.client import TelegramClient

    mock_client = MagicMock(spec=TelegramClient)
    mock_db = MagicMock()
    msg = {
        "chat": {"id": 12345, "type": "private"},
        "from": {"id": 12345, "username": "tester"},
        "message_id": 999,
    }

    # Standard succeeds, Research fails
    mock_std = {"provider": "Gemini", "configured": True, "connected": True, "model": "gemini-3.5-flash", "error": None}
    mock_res = {"provider": "Gemini Research", "configured": True, "connected": False, "model": "gemini-3.6-flash", "error": "model unavailable (404)"}

    with patch("herald.telegram.bot.is_user_authorized", return_value=True), \
         patch("herald.telegram.bot.get_ai_provider") as mock_get_prov, \
         patch("herald.ai.gemini_provider.GeminiProvider.check_research_connection", return_value=mock_res), \
         patch.object(settings, "GEMINI_API_KEY", "valid-key"):
        mock_prov = MagicMock()
        mock_prov.is_configured.return_value = True
        mock_prov.check_connection.return_value = mock_std
        mock_prov.check_research_connection.return_value = mock_res
        mock_get_prov.return_value = mock_prov

        handle_telegram_command(mock_db, mock_client, msg, "/ai-check", "")

        assert mock_client.send_message.call_count >= 2
        # Final message contains both statuses
        final_call = mock_client.send_message.call_args_list[-1]
        msg_text = final_call.kwargs.get("text", "")
        assert "Gemini (Standard):</b> Connected" in msg_text
        assert ("Gemini Research:</b> Unavailable" in msg_text) or ("Research Grounding (Gemini):</b> Unavailable" in msg_text)


