"""
Unit test matrix for Gemini script output truncation, adaptive retries,
thinking level configuration, finishReason/thought token telemetry,
and malformed vs truncated JSON classification.
"""

import json
import uuid
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from herald.config import settings
from herald.core.pipeline import process_herald_request
from herald.db.models import Base, JobState, PodcastJob
from herald.gemini.client import (
    GeminiOutputTruncatedError,
    _extract_tokens,
    _is_gemini_model_not_found_response,
    build_script_thinking_config,
    generate_podcast_script,
    get_gemini_max_output_tokens_ceiling,
)
from herald.services.redaction import (
    redact_dict,
    sanitize_content_dict,
    sanitize_error,
)


def _make_gemini_response(
    script_dict: dict | None = None,
    raw_text: str | None = None,
    finish_reason: str = "STOP",
    prompt_tokens: int = 2000,
    candidates_tokens: int = 1000,
    total_tokens: int = 3000,
    thought_tokens: int | None = 500,
    status_code: int = 200,
) -> httpx.Response:
    if raw_text is None:
        raw_text = json.dumps(
            script_dict
            or {
                "episode_title": "Test Episode",
                "episode_description": "Test Description",
                "estimated_minutes": 5,
                "source_title": "Test Source",
                "segments": [
                    {
                        "order": 1,
                        "heading": "Intro",
                        "narration": "This is test narration.",
                    }
                ],
                "warnings": [],
            }
        )

    body = {
        "candidates": [
            {
                "content": {"parts": [{"text": raw_text}], "role": "model"},
                "finishReason": finish_reason,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidates_tokens,
            "totalTokenCount": total_tokens,
        },
    }
    if thought_tokens is not None:
        body["usageMetadata"]["thoughtsTokenCount"] = thought_tokens

    return httpx.Response(status_code, json=body)


def test_extract_tokens_includes_thought_tokens():
    """Test _extract_tokens safely extracts prompt, candidates, total, and thought tokens."""
    data = {
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 200,
            "totalTokenCount": 350,
            "thoughtsTokenCount": 50,
        }
    }
    p, c, t, th = _extract_tokens(data)
    assert p == 100
    assert c == 200
    assert t == 350
    assert th == 50

    # Test nested candidatesTokensDetails fallback
    data_details = {
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 200,
            "totalTokenCount": 300,
            "candidatesTokensDetails": [{"modality": "THOUGHT", "tokenCount": 75}],
        }
    }
    p, c, t, th = _extract_tokens(data_details)
    assert th == 75

    # None handling
    assert _extract_tokens(None) == (None, None, None, None)


def test_build_script_thinking_config_matrix():
    """
    Test model contract for thinking configuration:
    - Brief + Gemini 3.x -> thinkingLevel=low
    - Standard + Gemini 3.x -> thinkingLevel=low
    - Brief/Standard + Gemini 2.5 -> thinkingBudget=1024
    - Research mode -> None (never forced to LOW thinking)
    - Unsupported/older models -> None
    """
    # 1. Gemini 3.x in brief/standard
    assert build_script_thinking_config("gemini-3.5-flash", "brief") == {"thinkingLevel": "low"}
    assert build_script_thinking_config("gemini-3.5-flash", "standard") == {"thinkingLevel": "low"}
    assert build_script_thinking_config("gemini-3.0-pro", "standard") == {"thinkingLevel": "low"}

    # 2. Gemini 2.5 in brief/standard
    assert build_script_thinking_config("gemini-2.5-flash", "brief") == {"thinkingBudget": 1024}
    assert build_script_thinking_config("gemini-2.5-flash", "standard") == {"thinkingBudget": 1024}
    assert build_script_thinking_config("gemini-2.5-pro", "standard") == {"thinkingBudget": 1024}
    assert build_script_thinking_config("gemini-2.0-flash-thinking-exp", "standard") == {"thinkingBudget": 1024}

    # 3. Research mode MUST NOT receive LOW thinking override
    assert build_script_thinking_config("gemini-3.5-flash", "research") is None
    assert build_script_thinking_config("gemini-2.5-flash", "research") is None

    # 4. Older/unsupported models
    assert build_script_thinking_config("gemini-1.5-flash", "standard") is None
    assert build_script_thinking_config("gemini-1.5-pro", "brief") is None
    assert build_script_thinking_config("gemini-1.0-pro", "standard") is None
    assert build_script_thinking_config("unknown-model", "standard") is None
    assert build_script_thinking_config(None, "standard") is None


def test_get_gemini_max_output_tokens_ceiling():
    """Test model family token ceilings."""
    assert get_gemini_max_output_tokens_ceiling("gemini-3.5-flash") == 65536
    assert get_gemini_max_output_tokens_ceiling("gemini-3.0-pro") == 65536
    assert get_gemini_max_output_tokens_ceiling("gemini-2.5-flash") == 65536
    assert get_gemini_max_output_tokens_ceiling("gemini-2.5-pro") == 65536
    assert get_gemini_max_output_tokens_ceiling("gemini-2.0-flash") == 8192
    assert get_gemini_max_output_tokens_ceiling("gemini-1.5-flash") == 8192
    assert get_gemini_max_output_tokens_ceiling("gemini-1.0-pro") == 4096
    assert get_gemini_max_output_tokens_ceiling("gemini-pro") == 4096
    assert get_gemini_max_output_tokens_ceiling("custom-unknown") is None
    assert get_gemini_max_output_tokens_ceiling(None) is None


def test_case_a_normal_valid_gemini_json_gemini_3x(monkeypatch):
    """
    Test Matrix A: Normal valid Gemini JSON with gemini-3.5-flash sends thinkingLevel=low.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "GEMINI_SCRIPT_MAX_OUTPUT_TOKENS", 16384)

    mock_resp = _make_gemini_response(finish_reason="STOP")

    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        return mock_resp

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("herald.gemini.client.record_ai_interaction") as mock_record:
        res = generate_podcast_script(source_text="Test source text", request_mode="standard", job_id="test-job-1")

    assert res.episode_title == "Test Episode"
    assert len(posted_payloads) == 1
    # Verify thinkingLevel: low is sent for gemini-3.5-flash
    gen_cfg = posted_payloads[0]["generationConfig"]
    assert gen_cfg["maxOutputTokens"] == 16384
    assert gen_cfg["thinkingConfig"] == {"thinkingLevel": "low"}

    # Verify telemetry recorded
    assert mock_record.called
    kwargs = mock_record.call_args[1]
    assert kwargs["success"] is True
    assert kwargs["metadata"]["finish_reason"] == "STOP"
    assert kwargs["metadata"]["thought_tokens"] == 500
    assert kwargs["metadata"]["requested_max_output_tokens"] == 16384


def test_case_b_normally_completed_malformed_json_triggers_repair_prompt(monkeypatch):
    """
    Test Matrix B: finishReason=STOP with malformed JSON triggers structured repair retry with error note in prompt.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "GEMINI_RETRY_COUNT", 2)

    # Attempt 1: malformed JSON but finishReason=STOP
    resp1 = _make_gemini_response(raw_text='{"episode_title": "Broken"', finish_reason="STOP")
    # Attempt 2: valid JSON
    resp2 = _make_gemini_response(finish_reason="STOP")

    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        if len(posted_payloads) == 1:
            return resp1
        return resp2

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("time.sleep"):
        res = generate_podcast_script(source_text="Test source text", job_id="test-job-2")

    assert res.episode_title == "Test Episode"
    assert len(posted_payloads) == 2
    # Verify attempt 2 prompt has the structured repair note
    attempt2_prompt = posted_payloads[1]["contents"][0]["parts"][0]["text"]
    assert "Previous attempt failed validation:" in attempt2_prompt


def test_case_c_and_d_truncated_json_adaptive_retry(monkeypatch):
    """
    Test Matrix C and D: finishReason=MAX_TOKENS with truncated JSON is classified as OUTPUT_TRUNCATED,
    does not send malformed prompt repair note, adaptively increases maxOutputTokens (16384 to 32768),
    and succeeds on second attempt.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "GEMINI_SCRIPT_MAX_OUTPUT_TOKENS", 16384)
    monkeypatch.setattr(settings, "GEMINI_RETRY_COUNT", 3)

    # Attempt 1: truncated JSON with finishReason=MAX_TOKENS
    resp1 = _make_gemini_response(
        raw_text='{"episode_title": "Truncated...',
        finish_reason="MAX_TOKENS",
        thought_tokens=2000,
    )
    # Attempt 2: valid JSON with finishReason=STOP
    resp2 = _make_gemini_response(finish_reason="STOP", thought_tokens=1024)

    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        if len(posted_payloads) == 1:
            return resp1
        return resp2

    recorded_calls = []

    def mock_record(*args, **kwargs):
        recorded_calls.append(kwargs)

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("herald.gemini.client.record_ai_interaction", side_effect=mock_record), \
         patch("time.sleep"):
        res = generate_podcast_script(source_text="Long article source", job_id="test-job-3")

    assert res.episode_title == "Test Episode"
    assert len(posted_payloads) == 2

    # Attempt 1 checked 16384
    assert posted_payloads[0]["generationConfig"]["maxOutputTokens"] == 16384
    # Attempt 2 increased budget to 32768
    assert posted_payloads[1]["generationConfig"]["maxOutputTokens"] == 32768

    # Attempt 2 prompt must NOT have malformed JSON repair note
    attempt2_prompt = posted_payloads[1]["contents"][0]["parts"][0]["text"]
    assert "Previous attempt failed validation:" not in attempt2_prompt

    # Verify attempt 1 was recorded as OUTPUT_TRUNCATED error category
    rec1 = recorded_calls[0]
    assert rec1["success"] is False
    assert rec1["error_category"] == "OUTPUT_TRUNCATED"
    assert rec1["metadata"]["finish_reason"] == "MAX_TOKENS"
    assert rec1["metadata"]["requested_max_output_tokens"] == 16384
    assert rec1["metadata"]["thought_tokens"] == 2000


def test_case_e_already_at_ceiling_fails_without_identical_retry(monkeypatch):
    """
    Test that when already at the model output token ceiling (65536) and finishReason=MAX_TOKENS,
    it does NOT issue a second request at the identical ceiling and fails immediately as GeminiOutputTruncatedError.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "GEMINI_SCRIPT_MAX_OUTPUT_TOKENS", 65536)
    monkeypatch.setattr(settings, "GEMINI_RETRY_COUNT", 3)

    resp_trunc = _make_gemini_response(
        raw_text='{"episode_title": "Never finishes...',
        finish_reason="MAX_TOKENS",
    )

    posted_calls = []

    def mock_post(url, json=None, headers=None):
        posted_calls.append(json)
        return resp_trunc

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("time.sleep"):
        with pytest.raises(GeminiOutputTruncatedError) as exc_info:
            generate_podcast_script(source_text="Massive article", job_id="test-job-4")

    # Only 1 call should have been made because doubling 65536 exceeds model ceiling 65536
    assert len(posted_calls) == 1
    assert "finishReason=MAX_TOKENS" in str(exc_info.value)
    cat, msg = sanitize_error(exc_info.value)
    assert cat in ("AI_OUTPUT_TRUNCATED", "OUTPUT_TRUNCATED")


def test_gemini_script_thinking_gemini_25(monkeypatch):
    """
    Test that Gemini 2.5 uses numeric thinkingBudget: 1024.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")

    mock_resp = _make_gemini_response(finish_reason="STOP")
    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        return mock_resp

    with patch("httpx.Client.post", side_effect=mock_post):
        generate_podcast_script(source_text="Test source text", request_mode="standard", job_id="test-job-5")

    assert len(posted_payloads) == 1
    gen_cfg = posted_payloads[0]["generationConfig"]
    assert gen_cfg["thinkingConfig"] == {"thinkingBudget": 1024}


def test_gemini_script_thinking_omitted_for_research(monkeypatch):
    """
    Test that Research mode script generation does NOT have LOW thinkingConfig forced upon it.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")

    mock_resp = _make_gemini_response(finish_reason="STOP")
    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        return mock_resp

    with patch("httpx.Client.post", side_effect=mock_post):
        generate_podcast_script(
            source_text="Test source text",
            request_mode="research",
            research_dossier={"summary": "Detailed research dossier"},
            job_id="test-job-6",
        )

    assert len(posted_payloads) == 1
    gen_cfg = posted_payloads[0]["generationConfig"]
    assert "thinkingConfig" not in gen_cfg


def test_gemini_script_thinking_omitted_for_unsupported_models(monkeypatch):
    """
    Test Matrix G: When configured with a model that does not support thinkingConfig (e.g. gemini-1.5-flash),
    thinkingConfig is cleanly omitted from payload.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-1.5-flash")

    mock_resp = _make_gemini_response(finish_reason="STOP")

    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        return mock_resp

    with patch("httpx.Client.post", side_effect=mock_post):
        generate_podcast_script(source_text="Test source text", job_id="test-job-7")

    assert len(posted_payloads) == 1
    gen_cfg = posted_payloads[0]["generationConfig"]
    assert "thinkingConfig" not in gen_cfg


def test_safe_numeric_telemetry_and_credential_redaction():
    """
    Test safe numeric telemetry allowlist:
    SAFE:
    - thought_tokens: 1500
    - requested_max_output_tokens: 16384
    - prompt_tokens: 3226
    - completion_tokens: 1200
    - total_tokens: 4426
    - finish_reason: "MAX_TOKENS"

    UNSAFE:
    - access_token: "abc123"
    - api_token: "abc123"
    - telegram_bot_token: "abc123"
    - prompt_tokens: "injected_string" (non-numeric token key)
    """
    raw_meta = {
        "thought_tokens": 1500,
        "requested_max_output_tokens": 16384,
        "prompt_tokens": 3226,
        "completion_tokens": 1200,
        "total_tokens": 4426,
        "finish_reason": "MAX_TOKENS",
        "access_token": "secret_access_token_value",
        "api_token": "secret_api_token_value",
        "telegram_bot_token": "secret_bot_token_value",
        "candidate_tokens": None,
    }

    clean = redact_dict(raw_meta)

    # Safe numeric values preserved
    assert clean["thought_tokens"] == 1500
    assert clean["requested_max_output_tokens"] == 16384
    assert clean["prompt_tokens"] == 3226
    assert clean["completion_tokens"] == 1200
    assert clean["total_tokens"] == 4426
    assert clean["candidate_tokens"] is None
    assert clean["finish_reason"] == "MAX_TOKENS"

    # Credentials redacted
    assert clean["access_token"] == "[REDACTED]"
    assert clean["api_token"] == "[REDACTED]"
    assert clean["telegram_bot_token"] == "[REDACTED]"

    # Non-numeric string in token telemetry key redacted
    smuggle_dict = {"prompt_tokens": "smuggled_string_value"}
    clean_smuggle = redact_dict(smuggle_dict)
    assert clean_smuggle["prompt_tokens"] == "[REDACTED]"

    # Test sanitize_content_dict
    clean_content = sanitize_content_dict(raw_meta)
    assert clean_content["thought_tokens"] == 1500
    assert clean_content["requested_max_output_tokens"] == 16384
    assert clean_content["access_token"] == "[REDACTED]"


def test_pipeline_output_truncated_propagation(monkeypatch):
    """
    Test pipeline-level propagation: when Gemini fails with GeminiOutputTruncatedError,
    the job state transitions to FAILED_FINAL with error_code = OUTPUT_TRUNCATED,
    and SCRIPTING_FAILED diagnostic event records error_category = OUTPUT_TRUNCATED.
    """
    from herald.core.pipeline import HeraldRequest

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "AI_PROVIDER", "gemini")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    db = TestingSession()

    def mock_trunc_gen(*args, **kwargs):
        raise GeminiOutputTruncatedError("Gemini output truncated by max_output_tokens limit (finishReason=MAX_TOKENS)")

    with patch("herald.gemini.client.generate_podcast_script", side_effect=mock_trunc_gen):
        req = HeraldRequest(
            source_text="Some long source article text",
            request_mode="standard",
        )
        resp = process_herald_request(db=db, req=req)

    assert resp.status == JobState.FAILED_FINAL.value
    assert "truncated" in resp.message.lower()

    # Query DB job
    job = db.query(PodcastJob).filter_by(id=resp.job_id).first()
    assert job is not None
    assert job.status == JobState.FAILED_FINAL.value
    assert job.error_code in ("AI_OUTPUT_TRUNCATED", "OUTPUT_TRUNCATED")
    assert "truncated" in job.error_detail.lower()
    db.close()


def test_brief_and_standard_default_to_16384_and_research_defaults_to_4096(monkeypatch):
    """
    Test that Brief and Standard script generation start at 16384 (GEMINI_SCRIPT_MAX_OUTPUT_TOKENS)
    while Research script generation starts at 4096 (GEMINI_MAX_OUTPUT_TOKENS) with no LOW thinking override.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "GEMINI_SCRIPT_MAX_OUTPUT_TOKENS", 16384)
    monkeypatch.setattr(settings, "GEMINI_MAX_OUTPUT_TOKENS", 4096)

    mock_resp = _make_gemini_response(finish_reason="STOP")
    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        return mock_resp

    with patch("httpx.Client.post", side_effect=mock_post):
        # 1. Brief mode
        generate_podcast_script(source_text="Brief text", request_mode="brief")
        # 2. Standard mode
        generate_podcast_script(source_text="Standard text", request_mode="standard")
        # 3. Research mode
        generate_podcast_script(
            source_text="Research text",
            request_mode="research",
            research_dossier={"dossier": "sample"},
        )

    assert len(posted_payloads) == 3

    # Brief: 16384, thinkingLevel=low
    brief_cfg = posted_payloads[0]["generationConfig"]
    assert brief_cfg["maxOutputTokens"] == 16384
    assert brief_cfg["thinkingConfig"] == {"thinkingLevel": "low"}

    # Standard: 16384, thinkingLevel=low
    std_cfg = posted_payloads[1]["generationConfig"]
    assert std_cfg["maxOutputTokens"] == 16384
    assert std_cfg["thinkingConfig"] == {"thinkingLevel": "low"}

    # Research: 4096, NO thinkingConfig override
    res_cfg = posted_payloads[2]["generationConfig"]
    assert res_cfg["maxOutputTokens"] == 4096
    assert "thinkingConfig" not in res_cfg


def test_research_mode_does_not_perform_adaptive_budget_doubling(monkeypatch):
    """
    Test that Research mode does not double maxOutputTokens on finishReason=MAX_TOKENS.
    It fails immediately with GeminiOutputTruncatedError.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "GEMINI_MAX_OUTPUT_TOKENS", 4096)
    monkeypatch.setattr(settings, "GEMINI_RETRY_COUNT", 3)

    resp_trunc = _make_gemini_response(
        raw_text='{"episode_title": "Truncated research...',
        finish_reason="MAX_TOKENS",
    )
    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        return resp_trunc

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("time.sleep"):
        with pytest.raises(GeminiOutputTruncatedError):
            generate_podcast_script(
                source_text="Research source text",
                request_mode="research",
                research_dossier={"dossier": "sample"},
            )

    # Must only make 1 call and NOT adaptively double budget
    assert len(posted_payloads) == 1
    assert posted_payloads[0]["generationConfig"]["maxOutputTokens"] == 4096


def test_model_ceiling_clamps_initial_request_and_retries(monkeypatch):
    """
    Test that when model ceiling (e.g. gemini-1.5-flash ceiling 8192) is lower than configured budget (16384),
    initial request is clamped to 8192, and no retry is sent since 8192 is already at ceiling.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-1.5-flash")
    monkeypatch.setattr(settings, "GEMINI_SCRIPT_MAX_OUTPUT_TOKENS", 16384)
    monkeypatch.setattr(settings, "GEMINI_RETRY_COUNT", 3)

    resp_trunc = _make_gemini_response(
        raw_text='{"episode_title": "Truncated...',
        finish_reason="MAX_TOKENS",
    )
    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        return resp_trunc

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("time.sleep"):
        with pytest.raises(GeminiOutputTruncatedError):
            generate_podcast_script(source_text="Text", request_mode="standard")

    assert len(posted_payloads) == 1
    # Clamped initial request to known ceiling 8192, NOT configured 16384
    assert posted_payloads[0]["generationConfig"]["maxOutputTokens"] == 8192


def test_unknown_model_does_not_adaptively_enlarge_beyond_known(monkeypatch):
    """
    Test that an unrecognized/unknown model does not perform adaptive enlargement beyond known capabilities.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "custom-unknown-model")
    monkeypatch.setattr(settings, "GEMINI_SCRIPT_MAX_OUTPUT_TOKENS", 16384)
    monkeypatch.setattr(settings, "GEMINI_RETRY_COUNT", 3)

    resp_trunc = _make_gemini_response(
        raw_text='{"episode_title": "Truncated...',
        finish_reason="MAX_TOKENS",
    )
    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        return resp_trunc

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("time.sleep"):
        with pytest.raises(GeminiOutputTruncatedError):
            generate_podcast_script(source_text="Text", request_mode="standard")

    assert len(posted_payloads) == 1
    assert posted_payloads[0]["generationConfig"]["maxOutputTokens"] == 16384


def test_thinking_telemetry_recorded_in_request_evidence(monkeypatch):
    """
    Test that requested thinking configuration (thinking_level or thinking_budget)
    is recorded cleanly into request_evidence and metadata.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")

    mock_resp = _make_gemini_response(finish_reason="STOP")
    recorded = []

    def mock_rec(*args, **kwargs):
        recorded.append(kwargs)

    with patch("httpx.Client.post", return_value=mock_resp), \
         patch("herald.gemini.client.record_ai_interaction", side_effect=mock_rec):
        generate_podcast_script(source_text="Text", request_mode="standard", job_id="job-think-1")

    assert len(recorded) == 1
    req_ev = recorded[0]["request_json"]
    assert req_ev["thinking_level"] == "low"
    meta = recorded[0]["metadata"]
    assert meta["thinking_level"] == "low"


def test_double_max_tokens_stops_at_two_requests_and_fails(monkeypatch):
    """
    Test Requirement 2:
    Attempt 1: 16384 -> MAX_TOKENS
    Attempt 2: 32768 -> MAX_TOKENS
    -> Fails immediately with GeminiOutputTruncatedError
    -> Exactly 2 API requests (does NOT make a 3rd request with 65536 despite retry count = 3).
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "GEMINI_SCRIPT_MAX_OUTPUT_TOKENS", 16384)
    monkeypatch.setattr(settings, "GEMINI_RETRY_COUNT", 3)

    resp_trunc1 = _make_gemini_response(
        raw_text='{"episode_title": "Truncated 1...',
        finish_reason="MAX_TOKENS",
    )
    resp_trunc2 = _make_gemini_response(
        raw_text='{"episode_title": "Truncated 2...',
        finish_reason="MAX_TOKENS",
    )

    posted_payloads = []

    def mock_post(url, json=None, headers=None):
        posted_payloads.append(json)
        if len(posted_payloads) == 1:
            return resp_trunc1
        return resp_trunc2

    with patch("httpx.Client.post", side_effect=mock_post), \
         patch("time.sleep"):
        with pytest.raises(GeminiOutputTruncatedError) as exc_info:
            generate_podcast_script(source_text="Long text", request_mode="standard")

    # Exactly 2 requests: 16384, then 32768
    assert len(posted_payloads) == 2
    assert posted_payloads[0]["generationConfig"]["maxOutputTokens"] == 16384
    assert posted_payloads[1]["generationConfig"]["maxOutputTokens"] == 32768
    assert "finishReason=MAX_TOKENS" in str(exc_info.value)


def test_max_tokens_with_empty_or_missing_parts_classified_as_output_truncated(monkeypatch):
    """
    Test Requirement 3:
    Gemini response with finishReason=MAX_TOKENS and empty parts array
    must record AI interaction with error_category = 'OUTPUT_TRUNCATED' and finish_reason = 'MAX_TOKENS'.
    """
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "GEMINI_SCRIPT_MAX_OUTPUT_TOKENS", 16384)
    monkeypatch.setattr(settings, "GEMINI_RETRY_COUNT", 1)

    empty_parts_body = {
        "candidates": [
            {
                "content": {"parts": [], "role": "model"},
                "finishReason": "MAX_TOKENS",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 2000,
            "candidatesTokenCount": 1000,
            "totalTokenCount": 3000,
            "thoughtsTokenCount": 500,
        },
    }
    resp_empty_parts = httpx.Response(200, json=empty_parts_body)

    recorded_calls = []

    def mock_record(*args, **kwargs):
        recorded_calls.append(kwargs)

    with patch("httpx.Client.post", return_value=resp_empty_parts), \
         patch("herald.gemini.client.record_ai_interaction", side_effect=mock_record), \
         patch("time.sleep"):
        with pytest.raises(GeminiOutputTruncatedError):
            generate_podcast_script(source_text="Test source", request_mode="standard", job_id="job-empty-parts-1")

    assert len(recorded_calls) >= 1
    rec = recorded_calls[0]
    assert rec["success"] is False
    assert rec["error_category"] == "OUTPUT_TRUNCATED"
    assert rec["metadata"]["finish_reason"] == "MAX_TOKENS"
    assert rec["metadata"]["requested_max_output_tokens"] == 16384
    assert rec["metadata"]["thought_tokens"] == 500


def test_is_gemini_model_not_found_response():
    """Test model 404 classifier distinguishes model not found from generic 404s."""
    # Model not found with NOT_FOUND status
    resp_not_found = httpx.Response(
        404,
        json={"error": {"code": 404, "message": "models/gemini-old is not found for API version v1beta", "status": "NOT_FOUND"}},
    )
    is_unavail, msg = _is_gemini_model_not_found_response(resp_not_found)
    assert is_unavail is True
    assert "models/gemini-old" in msg

    # Model not supported / deprecated
    resp_deprecated = httpx.Response(
        404,
        json={"error": {"code": 404, "message": "The requested model is no longer available", "status": "NOT_FOUND"}},
    )
    is_unavail, _ = _is_gemini_model_not_found_response(resp_deprecated)
    assert is_unavail is True

    # Generic 404 without model text
    resp_generic_404 = httpx.Response(
        404,
        json={"error": {"code": 404, "message": "Resource requested could not be found", "status": "UNKNOWN"}},
    )
    is_unavail, _ = _is_gemini_model_not_found_response(resp_generic_404)
    assert is_unavail is False

    # Non-404 status
    resp_500 = httpx.Response(500, json={"error": {"message": "Internal error"}})
    is_unavail, _ = _is_gemini_model_not_found_response(resp_500)
    assert is_unavail is False




class TestURLContextFallbackEligibility:
    """Verify URL Context fallback scenarios A through K."""

    def test_case_a_direct_403_fallback_succeeds_pipeline_continues(self, db_session: Session):
        """Case A: Direct 403 -> URL Context succeeds -> pipeline continues successfully."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("403 Forbidden", block_reason=BlockReason.PUBLIC_RETRIEVAL_BLOCK)
        mock_script = MagicMock()
        mock_script.model_dump.return_value = {
            "episode_title": "AI Breakthrough",
            "episode_description": "Summary of article",
            "estimated_minutes": 2,
            "segments": [{"segment_title": "Intro", "narration": "Narration text here."}],
        }

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch(
                "herald.gemini.client.extract_article_via_url_context",
                return_value={"title": "Extracted Title", "body": "This is the extracted body text of the article with enough words."},
            ) as mock_url_ctx,
            patch("herald.gemini.client.generate_podcast_script", return_value=mock_script),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=301,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://blocked.example.com/article-403",
                hold_for_approval=False,
            )
            response = process_herald_request(db=db_session, req=req)

        mock_url_ctx.assert_called_once()
        assert response.status == JobState.QUEUED_TTS.value
        job = db_session.query(PodcastJob).filter_by(id=response.job_id).first()
        assert job is not None
        assert job.status == JobState.QUEUED_TTS.value

    def test_case_b_url_context_failure_gives_paste_text_guidance(self, db_session: Session):
        """Case B: URL Context failure -> SOURCE_ACCESS_BLOCKED + paste-text guidance."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("403 Forbidden", block_reason=BlockReason.PUBLIC_RETRIEVAL_BLOCK)

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.gemini.client.extract_article_via_url_context", return_value=None) as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=302,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://blocked.example.com/article-failed-ctx",
            )
            response = process_herald_request(db=db_session, req=req)

        mock_url_ctx.assert_called_once()
        assert response.status == JobState.FAILED_FINAL.value
        assert response.error_category == "SOURCE_ACCESS_BLOCKED"
        assert "could not retrieve the original public page" in response.message
        assert "paste the article text directly" in response.message

    def test_case_c_literal_mode_zero_url_context_calls(self, db_session: Session):
        """Case C: Literal -> URL Context call count zero + actionable message."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("403 Forbidden", block_reason=BlockReason.PUBLIC_RETRIEVAL_BLOCK)

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=303,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="literal",
                source_url="https://blocked.example.com/article-literal",
            )
            response = process_herald_request(db=db_session, req=req)

        assert mock_url_ctx.call_count == 0
        assert response.status == JobState.FAILED_FINAL.value
        assert "Literal mode does not use AI-assisted URL retrieval" in response.message
        assert "paste the article text directly" in response.message

    def test_case_d_ssrf_zero_url_context_calls(self, db_session: Session):
        """Case D: SSRF -> URL Context call count zero."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import SSRFVulnerabilityError

        err = SSRFVulnerabilityError("Security violation: internal IP")

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=304,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="http://169.254.169.254/latest/meta-data",
            )
            response = process_herald_request(db=db_session, req=req)

        assert mock_url_ctx.call_count == 0
        assert response.status == JobState.FAILED_FINAL.value
        assert response.error_category == "SSRF_PROTECTION"

    def test_case_e_401_auth_required_zero_url_context_calls(self, db_session: Session):
        """Case E: 401 auth required -> URL Context call count zero."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("HTTP 401 Unauthorized", block_reason=BlockReason.AUTH_REQUIRED)

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=305,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://secret.example.com/article",
            )
            response = process_herald_request(db=db_session, req=req)

        assert mock_url_ctx.call_count == 0
        assert response.status == JobState.FAILED_FINAL.value

    def test_case_f_paywall_zero_url_context_calls(self, db_session: Session):
        """Case F: Paywall marker -> URL Context call count zero."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("Paywall detected", block_reason=BlockReason.PAYWALL)

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=306,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://paywall.example.com/article",
            )
            response = process_herald_request(db=db_session, req=req)

        assert mock_url_ctx.call_count == 0
        assert response.status == JobState.FAILED_FINAL.value

    def test_case_g_captcha_zero_url_context_calls(self, db_session: Session):
        """Case G: CAPTCHA marker -> URL Context call count zero."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("Captcha challenge", block_reason=BlockReason.CAPTCHA)

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=307,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://captcha.example.com/article",
            )
            response = process_herald_request(db=db_session, req=req)

        assert mock_url_ctx.call_count == 0
        assert response.status == JobState.FAILED_FINAL.value

    def test_case_h_direct_extraction_success_zero_url_context_calls(self, db_session: Session):
        """Case H: Direct extraction success -> URL Context call count zero."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request

        mock_script = MagicMock()
        mock_script.model_dump.return_value = {
            "episode_title": "Direct Extraction",
            "episode_description": "Summary",
            "estimated_minutes": 2,
            "segments": [{"segment_title": "Intro", "narration": "Direct narration text."}],
        }

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch(
                "herald.core.pipeline.extract_article_from_url",
                return_value=("Direct Title", "Direct body text with plenty of content.", "https://example.com/direct"),
            ),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.gemini.client.generate_podcast_script", return_value=mock_script),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=308,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://example.com/direct",
                hold_for_approval=False,
            )
            response = process_herald_request(db=db_session, req=req)

        assert mock_url_ctx.call_count == 0
        assert response.status == JobState.QUEUED_TTS.value

    def test_case_i_direct_http_telemetry_persisted(self, db_session: Session):
        """Case I: DIRECT_HTTP telemetry persisted in DB metrics and diagnostic events."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.db.models import JobDiagnosticEvent, JobProcessingMetric

        mock_script = MagicMock()
        mock_script.model_dump.return_value = {
            "episode_title": "Telemetry Direct",
            "episode_description": "Summary",
            "estimated_minutes": 1,
            "segments": [{"segment_title": "Intro", "narration": "Direct narration."}],
        }

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch(
                "herald.core.pipeline.extract_article_from_url",
                return_value=("Direct Title", "Direct body text for telemetry verification.", "https://example.com/telemetry-direct"),
            ),
            patch("herald.gemini.client.generate_podcast_script", return_value=mock_script),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=309,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://example.com/telemetry-direct",
                hold_for_approval=False,
            )
            response = process_herald_request(db=db_session, req=req)

        metric = (
            db_session.query(JobProcessingMetric)
            .filter_by(job_id=response.job_id, stage="URL_EXTRACTION")
            .first()
        )
        assert metric is not None
        assert metric.metadata_json.get("extraction_method") == "DIRECT_HTTP"

        event = (
            db_session.query(JobDiagnosticEvent)
            .filter_by(job_id=response.job_id, event_type="EXTRACTION_SUCCESS")
            .first()
        )
        assert event is not None
        assert event.metadata_json_sanitized.get("extraction_method") == "DIRECT_HTTP"

    def test_case_j_gemini_url_context_telemetry_persisted(self, db_session: Session):
        """Case J: GEMINI_URL_CONTEXT telemetry persisted in DB metrics and diagnostic events."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.db.models import JobDiagnosticEvent, JobProcessingMetric
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("403 Forbidden", block_reason=BlockReason.PUBLIC_RETRIEVAL_BLOCK)
        mock_script = MagicMock()
        mock_script.model_dump.return_value = {
            "episode_title": "Telemetry Fallback",
            "episode_description": "Summary",
            "estimated_minutes": 1,
            "segments": [{"segment_title": "Intro", "narration": "Fallback narration."}],
        }

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch(
                "herald.gemini.client.extract_article_via_url_context",
                return_value={"title": "Fallback Title", "body": "Fallback extracted article body content with sufficient length."},
            ),
            patch("herald.gemini.client.generate_podcast_script", return_value=mock_script),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=310,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://example.com/telemetry-fallback",
                hold_for_approval=False,
            )
            response = process_herald_request(db=db_session, req=req)

        metric = (
            db_session.query(JobProcessingMetric)
            .filter_by(job_id=response.job_id, stage="URL_EXTRACTION")
            .first()
        )
        assert metric is not None
        assert metric.metadata_json.get("extraction_method") == "GEMINI_URL_CONTEXT"
        assert metric.metadata_json.get("fallback_attempted") is True
        assert metric.metadata_json.get("fallback_result") == "SUCCESS"

        event = (
            db_session.query(JobDiagnosticEvent)
            .filter_by(job_id=response.job_id, event_type="EXTRACTION_SUCCESS")
            .first()
        )
        assert event is not None
        assert event.metadata_json_sanitized.get("extraction_method") == "GEMINI_URL_CONTEXT"
        assert event.metadata_json_sanitized.get("fallback_attempted") is True
        assert event.metadata_json_sanitized.get("fallback_result") == "SUCCESS"

    def test_case_k_url_context_ai_interaction_persisted(self, db_session: Session):
        """Case K: url_context_extraction AI interaction persisted via extract_article_via_url_context."""
        from herald.db.models import AIInteraction
        from herald.gemini.client import extract_article_via_url_context

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"x-goog-request-id": "req-ctx-123"}
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps({
                                    "title": "Valid Title",
                                    "body": "This is a valid extracted body that is well over one hundred characters long to ensure validation succeeds.",
                                })
                            }
                        ]
                    },
                    "urlContextMetadata": {
                        "urlMetadata": [
                            {
                                "retrievedUrl": "https://example.com/test-ai-interaction",
                                "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_SUCCESS",
                            }
                        ]
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 50,
                "candidatesTokenCount": 30,
                "totalTokenCount": 80,
            },
        }

        test_job_id = str(uuid.uuid4())
        job = PodcastJob(
            id=test_job_id,
            transport="api",
            source_hash="hash-ctx-ai",
            source_text="test",
            request_mode="standard",
            source_type="url",
            status=JobState.EXTRACTING.value,
        )
        db_session.add(job)
        db_session.commit()

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch("httpx.Client.post", return_value=mock_resp),
        ):
            result = extract_article_via_url_context(
                url="https://example.com/test-ai-interaction",
                job_id=test_job_id,
            )

        assert result is not None
        assert result["title"] == "Valid Title"

        interaction = (
            db_session.query(AIInteraction)
            .filter_by(job_id=test_job_id, operation="url_context_extraction")
            .first()
        )
        assert interaction is not None
        assert interaction.success is True
        assert interaction.metadata_json.get("finish_reason") == "STOP"
        assert interaction.metadata_json.get("requested_max_output_tokens") == settings.GEMINI_URL_CONTEXT_INITIAL_OUTPUT_TOKENS
        assert interaction.metadata_json.get("retrieval_status") == "URL_RETRIEVAL_STATUS_SUCCESS"

    def test_url_context_validation_failure_records_failure_telemetry(self, db_session: Session):
        """URL Context returning invalid/empty/short body records success=False and returns None."""
        from herald.db.models import AIInteraction
        from herald.gemini.client import extract_article_via_url_context

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"x-goog-request-id": "req-ctx-fail-456"}
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps({
                                    "title": "Short Page",
                                    "body": "Too short body text.",
                                })
                            }
                        ]
                    },
                    "urlContextMetadata": {
                        "urlMetadata": [
                            {
                                "retrievedUrl": "https://example.com/test-ai-fail",
                                "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_SUCCESS",
                            }
                        ]
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 50,
                "candidatesTokenCount": 10,
                "totalTokenCount": 60,
            },
        }

        test_job_id = str(uuid.uuid4())
        job = PodcastJob(
            id=test_job_id,
            transport="api",
            source_hash="hash-ctx-fail",
            source_text="test",
            request_mode="standard",
            source_type="url",
            status=JobState.EXTRACTING.value,
        )
        db_session.add(job)
        db_session.commit()

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch("httpx.Client.post", return_value=mock_resp),
        ):
            result = extract_article_via_url_context(
                url="https://example.com/test-ai-fail",
                job_id=test_job_id,
            )

        assert result is None

        interaction = (
            db_session.query(AIInteraction)
            .filter_by(job_id=test_job_id, operation="url_context_extraction")
            .first()
        )
        assert interaction is not None
        assert interaction.success is False
        assert "minimum 100 required" in (interaction.error_message or "")

    def test_url_context_metadata_cases_a_through_e(self, db_session: Session):
        """Test URL Context metadata: ERROR (A), PAYWALL (B), UNSAFE (C), MISSING (D) fail; SUCCESS (E) succeeds."""
        from herald.db.models import AIInteraction
        from herald.gemini.client import extract_article_via_url_context

        valid_body = "This is a legitimate article body retrieved by Gemini URL Context that has plenty of text and is well over one hundred characters long."

        cases = [
            ("URL_RETRIEVAL_STATUS_ERROR", False, "ERROR"),
            ("URL_RETRIEVAL_STATUS_PAYWALL", False, "PAYWALL"),
            ("URL_RETRIEVAL_STATUS_UNSAFE", False, "UNSAFE"),
            ("MISSING", False, "MISSING"),
            ("URL_RETRIEVAL_STATUS_SUCCESS", True, "SUCCESS"),
        ]

        for status_val, should_succeed, case_name in cases:
            job_id = str(uuid.uuid4())
            job = PodcastJob(
                id=job_id, transport="api", source_hash=f"hash-{case_name}", source_text="test",
                request_mode="standard", source_type="url", status=JobState.EXTRACTING.value,
            )
            db_session.add(job)
            db_session.commit()

            mock_cand = {
                "finishReason": "STOP",
                "content": {
                    "parts": [{"text": json.dumps({"title": f"Article {case_name}", "body": valid_body})}]
                },
            }
            if status_val != "MISSING":
                mock_cand["urlContextMetadata"] = {
                    "urlMetadata": [
                        {"retrievedUrl": f"https://example.com/{case_name}", "urlRetrievalStatus": status_val}
                    ]
                }

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {"x-goog-request-id": f"req-{case_name}"}
            mock_resp.json.return_value = {
                "candidates": [mock_cand],
                "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 30, "totalTokenCount": 80},
            }

            with (
                patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
                patch("httpx.Client.post", return_value=mock_resp),
            ):
                result = extract_article_via_url_context(
                    url=f"https://example.com/{case_name}",
                    job_id=job_id,
                )

            if should_succeed:
                assert result is not None, f"Case {case_name} should succeed"
                assert result["title"] == f"Article {case_name}"
            else:
                assert result is None, f"Case {case_name} should fail"

            interaction = db_session.query(AIInteraction).filter_by(job_id=job_id, operation="url_context_extraction").first()
            assert interaction is not None
            assert interaction.success is should_succeed

    def test_url_context_helper_rejects_private_url_without_http_calls(self):
        """Direct call to extract_article_via_url_context() rejects private URL before making any Gemini HTTP call."""
        from herald.gemini.client import extract_article_via_url_context

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch("httpx.Client.post") as mock_post,
        ):
            res_loopback = extract_article_via_url_context("http://127.0.0.1/private-data")
            assert res_loopback is None
            mock_post.assert_not_called()

            res_local = extract_article_via_url_context("http://localhost:8080/secret")
            assert res_local is None
            mock_post.assert_not_called()

            res_priv = extract_article_via_url_context("http://192.168.1.100/admin")
            assert res_priv is None
            mock_post.assert_not_called()

    def test_url_context_max_tokens_handling_and_adaptive_retry(self, db_session: Session):
        """MAX_TOKENS must never succeed. Adaptive retry doubles budget; repeated MAX_TOKENS fails cleanly."""
        from herald.db.models import AIInteraction
        from herald.gemini.client import extract_article_via_url_context

        job_id_a = str(uuid.uuid4())
        job_a = PodcastJob(
            id=job_id_a, transport="api", source_hash="hash-max-a", source_text="test",
            request_mode="standard", source_type="url", status=JobState.EXTRACTING.value,
        )
        db_session.add(job_a)
        db_session.commit()

        trunc_resp = MagicMock()
        trunc_resp.status_code = 200
        trunc_resp.headers = {"x-goog-request-id": "req-trunc"}
        trunc_resp.json.return_value = {
            "candidates": [
                {
                    "finishReason": "MAX_TOKENS",
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps({
                                    "title": "Truncated Title",
                                    "body": "This body happens to be valid JSON and over one hundred characters long, but the model truncated before completion so finishReason is MAX_TOKENS.",
                                })
                            }
                        ]
                    },
                    "urlContextMetadata": {
                        "urlMetadata": [
                            {"retrievedUrl": "https://example.com/trunc", "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_SUCCESS"}
                        ]
                    },
                }
            ],
            "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 30, "totalTokenCount": 80},
        }

        # Attempt 1 MAX_TOKENS, Attempt 2 STOP with valid content (Case B: succeeds on retry)
        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.headers = {"x-goog-request-id": "req-success"}
        success_resp.json.return_value = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps({
                                    "title": "Full Title",
                                    "body": "This is the full extracted body after the token budget doubled, successfully extracting the full content.",
                                })
                            }
                        ]
                    },
                    "urlContextMetadata": {
                        "urlMetadata": [
                            {"retrievedUrl": "https://example.com/trunc", "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_SUCCESS"}
                        ]
                    },
                }
            ],
            "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 50, "totalTokenCount": 100},
        }

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "GEMINI_URL_CONTEXT_INITIAL_OUTPUT_TOKENS", 4096),
            patch.object(settings, "GEMINI_URL_CONTEXT_MAX_OUTPUT_TOKENS", 16384),
            patch("time.sleep"),
            patch("httpx.Client.post", side_effect=[trunc_resp, success_resp]) as mock_post,
        ):
            res_retry = extract_article_via_url_context("https://example.com/trunc", job_id=job_id_a)
            assert res_retry is not None
            assert res_retry["title"] == "Full Title"
            assert mock_post.call_count == 2
            call2_payload = mock_post.call_args_list[1][1]["json"]
            assert call2_payload["generationConfig"]["maxOutputTokens"] == 8192

        # Case C: Repeated MAX_TOKENS at hard cap -> controlled failure
        job_id_c = str(uuid.uuid4())
        job_c = PodcastJob(
            id=job_id_c, transport="api", source_hash="hash-max-c", source_text="test",
            request_mode="standard", source_type="url", status=JobState.EXTRACTING.value,
        )
        db_session.add(job_c)
        db_session.commit()

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "GEMINI_RETRY_COUNT", 2),
            patch("time.sleep"),
            patch("httpx.Client.post", return_value=trunc_resp),
        ):
            res_fail = extract_article_via_url_context("https://example.com/trunc-fail", job_id=job_id_c)
            assert res_fail is None

        interactions = db_session.query(AIInteraction).filter_by(job_id=job_id_c).all()
        assert len(interactions) == 2
        for inter in interactions:
            assert inter.success is False
            assert "OUTPUT_TRUNCATED" in (inter.error_message or "")
            assert inter.metadata_json.get("finish_reason") == "MAX_TOKENS"

    def test_extract_article_403_classification_cases(self):
        """Prove real extract_article_from_url stream classifies 403 bodies into BlockReason."""
        from herald.extraction.url_extractor import (
            BlockReason,
            SourceAccessBlockedError,
            extract_article_from_url,
        )

        test_cases = [
            (b"<html><body>403 Forbidden: nginx</body></html>", BlockReason.PUBLIC_RETRIEVAL_BLOCK),
            (b"<html><head><title>Security Check</title></head><body>Please solve captcha to continue.</body></html>", BlockReason.CAPTCHA),
            (b"<html><head><title>Subscribe to read</title></head><body>This article is behind a paywall.</body></html>", BlockReason.PAYWALL),
            (b"<html><head><title>Just a moment...</title></head><body>Cloudflare anti-bot verification</body></html>", BlockReason.INTERSTITIAL),
        ]

        for body_bytes, expected_reason in test_cases:
            def mock_stream(method, url, **kwargs):
                mock_r = MagicMock()
                mock_r.status_code = 403
                mock_r.is_redirect = False
                mock_r.iter_bytes.return_value = [body_bytes]
                mock_r.headers = {"content-type": "text/html"}
                mock_ctx = MagicMock()
                mock_ctx.__enter__.return_value = mock_r
                mock_ctx.__exit__.return_value = None
                return mock_ctx

            with (
                patch("herald.extraction.url_extractor.validate_url_host", return_value=("example.com", 443, "93.184.216.34")),
                patch("httpx.Client.stream", side_effect=mock_stream),
            ):
                with pytest.raises(SourceAccessBlockedError) as exc_info:
                    extract_article_from_url("https://example.com/article")
                assert exc_info.value.block_reason == expected_reason, f"Expected {expected_reason} for body {body_bytes}"

    def test_extractor_to_pipeline_403_captcha_yields_zero_url_context_calls(self, db_session: Session):
        """When 403 response contains CAPTCHA marker, pipeline classifies as CAPTCHA and NEVER attempts URL Context."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request

        captcha_body = b"<html><title>Security Check</title><body>Please complete captcha</body></html>"

        def mock_stream(method, url, **kwargs):
            mock_r = MagicMock()
            mock_r.status_code = 403
            mock_r.is_redirect = False
            mock_r.iter_bytes.return_value = [captcha_body]
            mock_r.headers = {"content-type": "text/html"}
            mock_ctx = MagicMock()
            mock_ctx.__enter__.return_value = mock_r
            mock_ctx.__exit__.return_value = None
            return mock_ctx

        with (
            patch("herald.extraction.url_extractor.validate_url_host", return_value=("example.com", 443, "93.184.216.34")),
            patch("httpx.Client.stream", side_effect=mock_stream),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=555,
                requester_identity="telegram:555",
                delivery_target="555",
                request_mode="standard",
                source_url="https://example.com/captcha-blocked",
            )
            resp = process_herald_request(db=db_session, req=req)

        assert resp.status == JobState.FAILED_FINAL.value
        mock_url_ctx.assert_not_called()

    def test_sanitize_error_in_pipeline_diagnostics_is_string_not_tuple(self, db_session: Session):
        """Regression test: diagnostic events and direct_error in metrics must be strings, never tuple representations."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.db.models import JobDiagnosticEvent, JobProcessingMetric

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=ValueError("Test bad URL structure")),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=666,
                requester_identity="telegram:666",
                delivery_target="666",
                request_mode="standard",
                source_url="https://example.com/bad-extract",
            )
            resp = process_herald_request(db=db_session, req=req)

        assert resp.status == JobState.FAILED_FINAL.value
        db_session.expire_all()
        events = db_session.query(JobDiagnosticEvent).filter_by(job_id=resp.job_id).all()
        assert len(events) > 0
        for ev in events:
            assert not ev.message.startswith("('"), f"Event message should be string, got: {ev.message}"
            if ev.metadata_json_sanitized and "direct_error" in ev.metadata_json_sanitized:
                de = ev.metadata_json_sanitized["direct_error"]
                assert not str(de).startswith("('"), f"direct_error should be string, got: {de}"

        metrics = db_session.query(JobProcessingMetric).filter_by(job_id=resp.job_id).all()
        for m in metrics:
            if m.metadata_json and "direct_error" in m.metadata_json:
                de = m.metadata_json["direct_error"]
                assert not str(de).startswith("('"), f"Metric direct_error should be string, got: {de}"

    def test_performance_metrics_redacts_credentials_in_url_and_errors(self, db_session: Session):
        """Performance metrics sanitize_metadata redacts credentials from URLs, error messages, and nested dicts."""
        from herald.services.performance_metrics import sanitize_metadata

        raw_meta = {
            "url": "https://example.com/article?token=secrettoken123&api_key=apikey999",
            "direct_error": "Failed with Bearer mysecretbearer and x-api-key: supersecret",
            "extraction_method": "DIRECT_HTTP",
            "block_reason": "PUBLIC_RETRIEVAL_BLOCK",
            "prompt_tokens": 150,
            "success": True,
            "nested": {
                "sub_url": "https://api.internal/v1?secret=pass123",
                "count": 42,
            },
            "list_urls": [
                "https://example.com/a?key=secretkey1",
                "https://example.com/b?token=secrettoken2",
            ],
        }

        sanitized = sanitize_metadata(raw_meta)
        assert sanitized is not None
        assert "secrettoken123" not in str(sanitized)
        assert "apikey999" not in str(sanitized)
        assert "supersecret" not in str(sanitized)
        assert "pass123" not in str(sanitized)
        assert "secretkey1" not in str(sanitized)
        assert "secrettoken2" not in str(sanitized)
        # Preserves normal telemetry
        assert sanitized["extraction_method"] == "DIRECT_HTTP"
        assert sanitized["block_reason"] == "PUBLIC_RETRIEVAL_BLOCK"
        assert sanitized["prompt_tokens"] == 150
        assert sanitized["success"] is True
        assert sanitized["nested"]["count"] == 42



