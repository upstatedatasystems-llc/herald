"""
Unit tests for Priority 4: Provider Response Hardening & Kokoro Health-Probe Hardening.
Tests:
- Cloudflare Workers AI envelope extraction across all shapes:
  1. {"result": {"response": "..."}}
  2. {"result": {"output_text": "..."}}
  3. {"result": [{"response": "..."}]}
  4. {"result": "..."}
  5. Direct {"response": "..."}
  6. Choices format {"result": {"choices": [...]}} and top-level {"choices": [...]}
- Unparseable HTTP 200 classified as AIResponseInvalidError with safe diagnostics.
- Section expansion non-fatal resilience in long-form pipeline.
- Kokoro load-aware health check returning busy/degraded state during active synthesis.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from herald.ai.cloudflare_provider import (
    CloudflareProvider,
    _safe_payload_structure,
    extract_cloudflare_content,
)
from herald.ai.errors import AIResponseInvalidError
from herald.ai.long_form import EvidenceScope, execute_unified_long_form_pipeline
from herald.db.models import PodcastJob
from herald.tts.kokoro_client import KokoroClient


# ==============================================================================
# Cloudflare Workers AI Response Envelope Tests
# ==============================================================================

def test_extract_cloudflare_shape_result_response():
    data = {"result": {"response": "Generated script content A"}}
    assert extract_cloudflare_content(data) == "Generated script content A"


def test_extract_cloudflare_shape_result_output_text():
    data = {"result": {"output_text": "Generated script content B"}}
    assert extract_cloudflare_content(data) == "Generated script content B"


def test_extract_cloudflare_shape_result_list():
    data = {"result": [{"response": "Generated script content C"}]}
    assert extract_cloudflare_content(data) == "Generated script content C"

    data_text = {"result": [{"text": "Generated script content C2"}]}
    assert extract_cloudflare_content(data_text) == "Generated script content C2"


def test_extract_cloudflare_shape_result_string():
    data = {"result": "Generated script content D"}
    assert extract_cloudflare_content(data) == "Generated script content D"


def test_extract_cloudflare_shape_direct_response():
    data = {"response": "Generated script content E"}
    assert extract_cloudflare_content(data) == "Generated script content E"

    data_out = {"output_text": "Generated script content E2"}
    assert extract_cloudflare_content(data_out) == "Generated script content E2"


def test_extract_cloudflare_shape_choices():
    data_nested = {"result": {"choices": [{"message": {"content": "Generated script content F1"}}]}}
    assert extract_cloudflare_content(data_nested) == "Generated script content F1"

    data_top = {"choices": [{"message": {"content": "Generated script content F2"}}]}
    assert extract_cloudflare_content(data_top) == "Generated script content F2"


def test_extract_cloudflare_unparseable_envelope_raises_invalid_error():
    data = {"unrecognized_wrapper": {"unexpected_field": 12345}}
    with pytest.raises(AIResponseInvalidError) as exc_info:
        extract_cloudflare_content(data)

    err = exc_info.value
    assert err.category == "AI_RESPONSE_INVALID"
    assert err.retryable is True
    assert "Envelope structure" in err.safe_detail


def test_safe_payload_structure_does_not_leak_raw_text():
    data = {
        "result": {
            "secret_key": "secret_12345",
            "prompt_text": "Classified secret topic",
            "numbers": [1, 2, 3],
        }
    }
    structure = _safe_payload_structure(data)
    str_rep = str(structure)
    assert "secret_12345" not in str_rep
    assert "Classified secret topic" not in str_rep
    assert structure["result"]["type"] == "dict"
    assert "secret_key" in structure["result"]["keys"]


# ==============================================================================
# Long-Form Section Expansion Resilience Tests
# ==============================================================================

def test_long_form_section_expansion_fails_non_fatally():
    """Verify that if optional continuation/expansion fails, the long form pipeline still succeeds with base outline."""
    job = PodcastJob(
        id="test-job-exp-resilience-1",
        transport="telegram",
        status="PENDING",
        content_mode="expanded",
        request_mode="standard",
        target_minutes="45",  # Explicit budget to trigger continuation pass
        source_text="This is a solid article about technology breakthroughs in sustainable energy." * 10,
        ai_provider="gemini",
        ai_model="gemini-3.5-flash",
    )

    db = MagicMock()

    def mock_grounding(p, att, src):
        return {"search_count": 2, "source_count": 1, "raw_text": "Energy research"}

    # Mock execute_with_failover:
    # 1. grounded_research succeeds
    # 2. section_generation calls: base sections succeed; continuation section fails
    calls = []

    def mock_failover(job, operation, execute_fn, **kwargs):
        calls.append(operation)
        if operation == "grounded_research":
            return {"search_count": 2, "source_count": 1, "raw_text": "Energy research"}
        elif operation == "section_generation":
            mock_resp = MagicMock()
            mock_seg = MagicMock()
            # Underfill words to trigger continuation
            mock_seg.narration = "Short section narration."
            mock_resp.segments = [mock_seg]
            mock_resp.episode_title = "Energy Future"
            mock_resp.episode_description = "Episode on energy"
            return mock_resp
        elif operation == "verification":
            mock_audit = MagicMock()
            mock_audit.has_material_issues = False
            mock_audit.repair_instructions = None
            mock_audit.model_dump.return_value = {"has_material_issues": False}
            return mock_audit
        return MagicMock()

    # Make generate_single_section raise on the continuation section (section_index > len(sections))
    original_gen_single = None

    def mock_gen_single(*args, **kwargs):
        sec_info = kwargs.get("section_info", {})
        if "Comprehensive Analysis and Evidence Synthesis" in sec_info.get("heading", ""):
            raise RuntimeError("Cloudflare AI timeout during optional continuation expansion")
        mock_resp = MagicMock()
        mock_seg = MagicMock()
        mock_seg.narration = "Short section narration."
        return {
            "section_index": sec_info.get("section_index", 1),
            "heading": sec_info.get("heading", "Intro"),
            "narration": "Short section narration.",
            "word_count": 3,
        }

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover), \
         patch("herald.ai.long_form.generate_single_section", side_effect=mock_gen_single), \
         patch("herald.ai.long_form.record_job_diagnostic_event"):
        res = execute_unified_long_form_pipeline(
            db=db,
            job=job,
            topic="Energy Future",
            scope=EvidenceScope.SOURCE_PLUS_RESEARCH,
            target_minutes="45",
            research_depth="medium",
            source_text=job.source_text,
        )

    # Invariant: Job completed successfully without being terminated by optional expansion failure
    assert res is not None
    assert len(job.section_progress_json) > 0


# ==============================================================================
# Kokoro Load-Aware Health Probe Tests
# ==============================================================================

def test_kokoro_health_probe_returns_degraded_healthy_during_active_synthesis():
    client = KokoroClient(base_url="http://kokoro:8880/v1")

    # Simulate active synthesis underway
    with KokoroClient._active_syntheses_lock:
        KokoroClient._active_syntheses = 1

    try:
        # Mock FFmpeg available, /models timeout
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("httpx.Client.get", side_effect=httpx.TimeoutException("Read timeout")):
            status = client.health_check()

        # Invariant: Must recognize active synthesis and return healthy=True, degraded=True
        assert status["healthy"] is True
        assert status["degraded"] is True
        assert status["kokoro_api"] is True
    finally:
        with KokoroClient._active_syntheses_lock:
            KokoroClient._active_syntheses = 0


def test_kokoro_health_probe_fails_on_connection_error():
    client = KokoroClient(base_url="http://kokoro:8880/v1")

    with KokoroClient._active_syntheses_lock:
        KokoroClient._active_syntheses = 0

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("httpx.Client.get", side_effect=httpx.ConnectError("Connection refused")):
        status = client.health_check()

    assert status["healthy"] is False
    assert status["kokoro_api"] is False
    assert "connection failed" in str(status["error"]).lower()


def test_kokoro_health_probe_fails_when_idle_and_grace_expired():
    client = KokoroClient(base_url="http://kokoro:8880/v1")

    with KokoroClient._active_syntheses_lock:
        KokoroClient._active_syntheses = 0
    KokoroClient._last_successful_probe_at = None

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("httpx.Client.get", side_effect=httpx.TimeoutException("Timeout")):
        status = client.health_check()

    assert status["healthy"] is False
    assert status["kokoro_api"] is False
    assert "timeout" in str(status["error"]).lower()
