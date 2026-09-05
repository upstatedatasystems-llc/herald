"""
Unit test matrix for Gemini script output truncation, adaptive retries,
thinking level configuration, finishReason/thought token telemetry,
and malformed vs truncated JSON classification.
"""

import json
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.core.pipeline import process_herald_request
from herald.db.models import Base, JobState, PodcastJob
from herald.gemini.client import (
    GeminiOutputTruncatedError,
    _extract_tokens,
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
    assert cat == "OUTPUT_TRUNCATED"


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
    assert job.error_code == "OUTPUT_TRUNCATED"
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
