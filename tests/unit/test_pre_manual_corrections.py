"""
Unit and regression tests for HERALD - Final Pre-Manual-Test Correction Pass.
Tests cover:
1. Voice cache rebuild and --force semantics.
2. Reachable first-chunk progress milestone retry on subsequent chunks.
3. Wall-clock bounded total diagnostic deadline and DNS timeout.
4. Dedicated trusted internal Kokoro health probe vs public URL anti-SSRF refusal.
5. Truthful AI model, provider, and operation tracking in failure diagnostics.
6. Case-D rerun approval failure full failed-job card UX.
7. Strict transport-level idempotency for COMPLETE jobs (no audio redelivery on replay).
8. HTML-escaping of variable content in format_concise_failure_summary.
"""

import html
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import Base, JobState, PodcastJob, RequestMode
from herald.services.failure_diagnostics import (
    _probe_network_target,
    _probe_trusted_kokoro,
    _resolve_dns_bounded,
    collect_failure_diagnostics,
    format_concise_failure_summary,
)
from herald.services.progress_notifier import notify_tts_chunk_progress
from herald.services.voice_manager import (
    compute_sample_text_hash,
    ensure_voice_sample,
    get_cached_voice_sample,
    get_voice_sample_path,
    load_voice_sample_manifest,
    prewarm_all_voice_samples,
    save_voice_sample_manifest,
)
from herald.telegram.client import TelegramClient
from herald.telegram.formatters import format_first_chunk_progress, format_generation_failure_card


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


# ======================================================================
# Item 1: Voice cache rebuild and --force semantics
# ======================================================================

def test_voice_cache_rebuild_and_force_semantics(tmp_path, monkeypatch):
    """
    Verify:
    - An orphan/legacy audio file without a current manifest entry is rejected by get_cached_voice_sample.
    - ensure_voice_sample(force=False) repairs/rebuilds and updates the manifest.
    - Subsequent call with force=False returns the cached sample without re-synthesizing.
    - prewarm_all_voice_samples(force=True) forces re-synthesis and updates the manifest.
    """
    samples_dir = tmp_path / "voice_samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("herald.services.voice_manager.get_voice_samples_dir", lambda: samples_dir)
    monkeypatch.setattr(settings, "ALLOWED_VOICES", "af_heart")

    orphan_mp3 = get_voice_sample_path("af_heart")
    orphan_mp3.write_bytes(b"ID3fake_mp3_content_longer_than_32_bytes_header_padding")

    # Audio validity mock (returns True for our fake MP3)
    monkeypatch.setattr("herald.services.voice_manager.is_valid_sample_audio", lambda p: True)

    # 1. Manifest is empty -> get_cached_voice_sample must reject orphan file
    assert get_cached_voice_sample("af_heart") is None

    # Mock KokoroClient synthesis and FFmpeg conversion
    mock_kokoro = MagicMock()
    synth_calls = []

    def mock_synthesize_chunk(text, output_path, voice, speed=1.0, timeout=180.0):
        synth_calls.append(voice)
        Path(output_path).write_bytes(b"RIFFfake_wav_data_padding_for_audio_test")
        return {"audio_path": output_path, "duration": 1.5}

    mock_kokoro.synthesize_chunk.side_effect = mock_synthesize_chunk

    def mock_convert(wav, mp3):
        mp3.write_bytes(b"ID3fake_converted_mp3_bytes_padding_12345")
        return mp3

    monkeypatch.setattr("herald.services.voice_manager.convert_wav_to_mp3", mock_convert)

    # 2. ensure_voice_sample(force=False) should notice cache miss, synthesize, and write manifest
    res_path = ensure_voice_sample("af_heart", kokoro_client=mock_kokoro, force=False)
    assert res_path == orphan_mp3
    assert len(synth_calls) == 1

    # Manifest should now be populated
    manifest = load_voice_sample_manifest()
    assert "af_heart" in manifest
    assert manifest["af_heart"]["voice_id"] == "af_heart"
    assert manifest["af_heart"]["format"] == "mp3"
    assert manifest["af_heart"]["speed"] == 1.0

    # 3. Subsequent ensure_voice_sample(force=False) hits cache; synthesis is NOT called again
    res_path2 = ensure_voice_sample("af_heart", kokoro_client=mock_kokoro, force=False)
    assert res_path2 == orphan_mp3
    assert len(synth_calls) == 1

    # 4. prewarm_all_voice_samples(force=True) forces re-synthesis
    prewarm_res = prewarm_all_voice_samples(kokoro_client=mock_kokoro, force=True)
    assert prewarm_res.get("af_heart") is True
    assert len(synth_calls) == 2


# ======================================================================
# Item 2: Reachable first-chunk milestone retry on subsequent chunks
# ======================================================================

def test_first_chunk_milestone_retry_on_later_chunks(db_session):
    """
    Verify:
    - Chunk 1 milestone fails delivery (e.g. transient network error). Claim is cleared.
    - Chunk 2 milestone retries and succeeds, formatting dynamic chunk progress.
    - Chunk 3 milestone does not resend because telegram_progress_message_id is now populated.
    """
    job = PodcastJob(
        id="job-retry-milestone",
        transport="telegram",
        telegram_chat_id=1001,
        telegram_message_id=2001,
        request_mode="standard",
        status=JobState.SYNTHESIZING.value,
        source_hash="hash-retry-123",
        source_text="source text for testing retry",
        custom_voice="af_bella",
        custom_speed=1.0,
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.is_configured = True

    # 1. Chunk 1 attempt fails
    mock_client.send_message.return_value = None  # delivery failed
    res1 = notify_tts_chunk_progress(
        db=db_session,
        job=job,
        chunk_index=1,
        total_chunks=4,
        chunk_audio_duration_s=10.0,
        chunk_synthesis_duration_s=5.0,
        telegram_client=mock_client,
    )
    assert res1 is False
    db_session.refresh(job)
    assert job.telegram_progress_message_id is None
    # Claim should have been cleared for retry
    assert job.first_chunk_progress_claimed_at is None

    # 2. Chunk 2 attempt succeeds
    mock_client.send_message.return_value = {"message_id": 777}
    res2 = notify_tts_chunk_progress(
        db=db_session,
        job=job,
        chunk_index=2,
        total_chunks=4,
        chunk_audio_duration_s=10.0,
        chunk_synthesis_duration_s=5.0,
        telegram_client=mock_client,
    )
    assert res2 is True
    db_session.refresh(job)
    assert job.telegram_progress_message_id == 777

    # Check text sent on chunk 2 reflects multi-chunk progress
    sent_text = mock_client.send_message.call_args[1]["text"]
    assert "2/4 segments completed" in sent_text

    # 3. Chunk 3 does NOT resend
    mock_client.send_message.reset_mock()
    res3 = notify_tts_chunk_progress(
        db=db_session,
        job=job,
        chunk_index=3,
        total_chunks=4,
        chunk_audio_duration_s=10.0,
        chunk_synthesis_duration_s=5.0,
        telegram_client=mock_client,
    )
    assert res3 is False
    mock_client.send_message.assert_not_called()


# ======================================================================
# Item 3: Wall-clock bounded total diagnostic deadline
# ======================================================================

def test_wall_clock_bounded_dns_and_timeout():
    """
    Verify:
    - _resolve_dns_bounded uses a daemon thread and returns within the specified timeout if getaddrinfo hangs.
    - _probe_network_target records TIMEOUT and skips TCP connect if deadline expires after DNS.
    """
    # 1. Test daemon thread DNS timeout
    def hanging_getaddrinfo(*args, **kwargs):
        time.sleep(2.0)
        return []

    with patch("socket.getaddrinfo", hanging_getaddrinfo):
        t0 = time.monotonic()
        res, err = _resolve_dns_bounded("slow-host.example.com", 80, timeout=0.1)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.8
        assert res is None
        assert isinstance(err, TimeoutError)

    # 2. Test deadline expiration before TCP connect
    mock_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
    with patch("herald.services.failure_diagnostics._resolve_dns_bounded", return_value=(mock_addrinfo, None)), \
         patch("time.monotonic", side_effect=[100.0, 100.0, 105.0, 105.0]), \
         patch("socket.socket") as mock_sock:
        # timeout_seconds=1.0, but time.monotonic jumps by 5.0s during DNS resolution
        probe_res = _probe_network_target("http://example.com/test", timeout_seconds=1.0)
        assert probe_res["status"] == "TIMEOUT"
        assert "Timed out before TCP probe" in probe_res["summary"]
        mock_sock.assert_not_called()


# ======================================================================
# Item 4: Dedicated trusted Kokoro probe vs public URL SSRF refusal
# ======================================================================

def test_trusted_kokoro_probe_vs_public_ssrf():
    """
    Verify:
    - _probe_trusted_kokoro allows private Docker network IP (e.g. 172.18.0.5) and checks reachability.
    - _probe_network_target refuses private IP with SSRF_REFUSAL without opening socket.
    """
    mock_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.5", 8880))]

    with patch("herald.services.failure_diagnostics._resolve_dns_bounded", return_value=(mock_addrinfo, None)), \
         patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock

        # Kokoro probe should succeed and return HEALTHY
        kokoro_res = _probe_trusted_kokoro(timeout_seconds=1.0)
        assert kokoro_res["status"] == "HEALTHY"
        assert kokoro_res["target_ip"] == "172.18.0.5"
        mock_sock.connect.assert_called_once_with(("172.18.0.5", 8880))

    # Generic probe against user target on 172.18.0.5 must be refused as SSRF
    with patch("herald.services.failure_diagnostics._resolve_dns_bounded", return_value=(mock_addrinfo, None)), \
         patch("socket.socket") as mock_sock_cls2:
        public_res = _probe_network_target("http://172.18.0.5/article", timeout_seconds=1.0)
        assert public_res["status"] == "SSRF_REFUSAL"
        assert "Blocked prohibited IP" in public_res["summary"]
        mock_sock_cls2.assert_not_called()


# ======================================================================
# Item 5: AI model, provider, and operation in failure diagnostics
# ======================================================================

def test_accurate_ai_model_and_operation_in_failure_diagnostics():
    """
    Verify:
    - Grounded research records provider='gemini', configured_model=settings.GEMINI_RESEARCH_MODEL, operation='grounded_research'.
    - Alternative provider records exact provider name and model.
    - Literal mode records no AI diagnostics.
    """
    # 1. Grounded research
    res_research = collect_failure_diagnostics(
        stage="research",
        error=Exception("Google Search grounding quota exceeded"),
        provider="gemini",
        model=getattr(settings, "GEMINI_RESEARCH_MODEL", "gemini-3.6-flash"),
        operation="grounded_research",
    )
    ai_diag = res_research.get("ai_diagnostics")
    assert ai_diag is not None
    assert ai_diag["provider"] == "gemini"
    assert ai_diag["configured_model"] == getattr(settings, "GEMINI_RESEARCH_MODEL", "gemini-3.6-flash")
    assert ai_diag["operation"] == "grounded_research"

    # 2. Alternative provider
    res_alt = collect_failure_diagnostics(
        stage="scripting",
        error=Exception("Provider rate limited"),
        provider="openrouter",
        model="anthropic/claude-3.5-sonnet",
        operation="standard_script",
    )
    ai_diag_alt = res_alt.get("ai_diagnostics")
    assert ai_diag_alt is not None
    assert ai_diag_alt["provider"] == "openrouter"
    assert ai_diag_alt["configured_model"] == "anthropic/claude-3.5-sonnet"
    assert ai_diag_alt["operation"] == "standard_script"

    # 3. Literal mode
    res_literal = collect_failure_diagnostics(
        stage="scripting",
        error=Exception("Text normalization error"),
        operation="literal_script",
    )
    assert res_literal.get("ai_diagnostics") is None


# ======================================================================
# Item 6: Case-D rerun approval failure full failed-job card UX
# ======================================================================

def test_case_d_rerun_approval_failure_card():
    """
    Verify format_generation_failure_card renders:
    - Status: FAILED_FINAL
    - Job ID
    - Concise diagnostic summary
    - Copyable /diagnostics <job-id>
    """
    job = PodcastJob(
        id="job-cased-fail-12345",
        status=JobState.FAILED_FINAL.value,
        error_detail="Script generation LLM call failed",
        auto_diagnostics_json=[{
            "stage": "scripting",
            "error_category": "AI_MODEL_UNAVAILABLE",
            "summary": "Gemini [gemini-3.5-flash]: Model Unavailable (404)",
        }],
    )

    card = format_generation_failure_card(job=job)
    assert "❌ <b>Podcast Generation Failed</b>" in card
    assert "• <b>ID:</b> <code>job-case</code>" in card
    assert f"• <b>Status:</b> <code>{JobState.FAILED_FINAL.value}</code>" in card
    assert "Script generation LLM call failed" in card
    assert "• <b>Diagnostic:</b> Gemini [gemini-3.5-flash]: Model Unavailable (404)" in card
    assert "Use <code>/diagnostics job-case</code> for support details." in card


# ======================================================================
# Item 7: Strict transport-level idempotency for COMPLETE jobs
# ======================================================================

def test_transport_idempotency_for_completed_jobs(db_session, tmp_path):
    """
    Verify:
    - When an exact transport replay of a COMPLETE job arrives (same telegram message),
      Herald does NOT send audio or redelivery messages to the user.
    """
    audio_file = tmp_path / "test_audio.mp3"
    audio_file.write_bytes(b"ID3mock_audio_content")

    job = PodcastJob(
        id="job-completed-idemp",
        transport="telegram",
        telegram_chat_id=54321,
        telegram_message_id=9876,
        status=JobState.COMPLETE.value,
        source_url="https://example.com/article",
        source_hash="hash-complete-idemp",
        source_text="Test source text content",
        local_audio_path=str(audio_file),
    )
    db_session.add(job)
    db_session.commit()

    from herald.telegram.bot import handle_telegram_content_message

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.is_configured = True

    # Replay of the exact same Telegram message
    message_data = {
        "message_id": 9876,
        "chat": {"id": 54321, "type": "private"},
        "from": {"id": 111, "first_name": "Test"},
        "text": "https://example.com/article",
    }

    with patch("herald.telegram.bot.is_user_authorized", return_value=True):
        handle_telegram_content_message(
            db=db_session,
            client=mock_client,
            message=message_data,
        )

    # Must NOT call send_audio or send_message (no re-delivery on transport replay)
    mock_client.send_audio.assert_not_called()
    mock_client.send_message.assert_not_called()


# ======================================================================
# Item 8: HTML escaping in format_concise_failure_summary
# ======================================================================

def test_html_escaping_in_format_concise_failure_summary():
    """
    Verify format_concise_failure_summary HTML-escapes raw summary, stage, and error_category
    to prevent Telegram 400 Bad Request entity parsing errors.
    """
    diag_record = {
        "stage": "ai<script>",
        "error_category": "PARSE_ERROR & ABORT",
        "summary": "Error in <stdin> line 42: <unmatched tag> & invalid <syntax>",
    }

    result = format_concise_failure_summary(diag_record)
    assert "&lt;stdin&gt;" in result
    assert "&lt;unmatched tag&gt;" in result
    assert "&amp;" in result
    assert "<stdin>" not in result
    assert "<unmatched tag>" not in result

    # Also test fallback path when summary is empty
    fallback_record = {
        "stage": "ai<script>",
        "error_category": "ERR <TAG> & CRASH",
        "summary": "",
    }
    fb_result = format_concise_failure_summary(fallback_record)
    assert "&lt;SCRIPT&gt;" in fb_result
    assert "ERR &lt;TAG&gt; &amp; CRASH" in fb_result
    assert "<script>" not in fb_result
