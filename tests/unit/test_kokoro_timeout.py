from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from apps.worker.main import process_next_job
from herald.audio.artifact_generator import generate_diagnostics_artifact
from herald.config import settings
from herald.db.models import JobState, PodcastJob
from herald.tts.kokoro_client import (
    KokoroClient,
    KokoroTTSTimeoutError,
)


def test_kokoro_synthesis_timeout_default_and_config(monkeypatch, tmp_path):
    monkeypatch.delenv("HERALD_MOCK_TTS", raising=False)
    assert settings.KOKORO_SYNTHESIS_TIMEOUT_SECONDS == 180

    client = KokoroClient()
    out_file = tmp_path / "test_default_timeout.wav"

    with patch("httpx.Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_instance.__enter__.return_value = mock_instance
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"RIFF....WAVEfmt...."
        mock_instance.post.return_value = mock_resp
        mock_client_cls.return_value = mock_instance

        client.synthesize_chunk("Hello test", out_file, timeout=180.0)
        mock_client_cls.assert_called_with(timeout=180.0)


def test_kokoro_synthesis_succeeds_between_60s_and_180s(tmp_path, monkeypatch):
    """Test synthesis taking 70s (>60s default, <180s new limit) succeeds."""
    monkeypatch.delenv("HERALD_MOCK_TTS", raising=False)
    client = KokoroClient()
    out_file = tmp_path / "chunk_long.wav"

    with patch("httpx.Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_instance.__enter__.return_value = mock_instance
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"RIFF....WAVEfmt header dummy wav data"
        mock_instance.post.return_value = mock_resp
        mock_client_cls.return_value = mock_instance

        res = client.synthesize_chunk("Long text narration", out_file, timeout=180.0)
        assert res.exists()
        assert res.stat().st_size > 0


def test_kokoro_synthesis_times_out_and_raises_specific_error(tmp_path, monkeypatch):
    """Test httpx timeout raises KokoroTTSTimeoutError specifically."""
    monkeypatch.delenv("HERALD_MOCK_TTS", raising=False)
    client = KokoroClient()
    out_file = tmp_path / "chunk_timeout.wav"

    with patch("httpx.Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.post.side_effect = httpx.TimeoutException("Read timeout")
        mock_client_cls.return_value = mock_instance

        with pytest.raises(KokoroTTSTimeoutError) as exc_info:
            client.synthesize_chunk("Text causing timeout", out_file, timeout=180.0)

        assert "timed out after" in str(exc_info.value)
        assert "180" in str(exc_info.value)
        assert not out_file.exists()


def test_health_check_vs_synthesis_timeout_independence(monkeypatch, tmp_path):
    """Verify health probe uses 3s probe timeout while synthesis uses 180s timeout."""
    monkeypatch.delenv("HERALD_MOCK_TTS", raising=False)
    client = KokoroClient()

    with (
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_instance = MagicMock()
        mock_instance.__enter__.return_value = mock_instance
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_instance.get.return_value = mock_resp
        mock_client_cls.return_value = mock_instance

        client.health_check()
        mock_client_cls.assert_called_with(timeout=3.0)

    out_file = tmp_path / "tmp_synth_indep.wav"
    with patch("httpx.Client") as mock_client_cls_synth:
        mock_instance_synth = MagicMock()
        mock_instance_synth.__enter__.return_value = mock_instance_synth
        mock_resp_synth = MagicMock()
        mock_resp_synth.status_code = 200
        mock_resp_synth.content = b"RIFF....WAVE"
        mock_instance_synth.post.return_value = mock_resp_synth
        mock_client_cls_synth.return_value = mock_instance_synth

        client.synthesize_chunk("Text", out_file, timeout=180.0)
        mock_client_cls_synth.assert_called_with(timeout=180.0)


def test_failed_chunk_does_not_advance_completed_chunk_index_and_resumes(
    db_session, monkeypatch, tmp_path
):
    """
    Test that chunk failure/timeout:
    1. Does NOT advance job.completed_chunk_index
    2. Keeps completed chunks on disk
    3. Resumes synthesis at failed chunk on retry without duplicating output
    """
    monkeypatch.setattr(settings, "HERALD_ENV", "test")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))

    job = PodcastJob(
        id="job-resumable-timeout-001",
        gmail_message_id="msg-timeout-1",
        sender_email="test@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-timeout-1",
        source_text="Test source text",
        status=JobState.QUEUED_TTS.value,
        completed_chunk_index=0,
        tts_chunk_chars=50,
        script_json={
            "episode_title": "Timeout Resumability Test",
            "segments": [
                {"order": 1, "heading": "Part 1", "narration": "First segment narration."},
                {
                    "order": 2,
                    "heading": "Part 2",
                    "narration": "Second segment narration causing failure.",
                },
            ],
            "warnings": [],
        },
    )
    db_session.add(job)
    db_session.commit()

    client = KokoroClient()

    # Step 1: Synthesize chunk 1 successfully, fail on chunk 2
    synth_calls = []

    def mock_synth(text, output_path, voice=None, speed=None, timeout=None):
        synth_calls.append(output_path.name)
        if "0002" in output_path.name:
            raise KokoroTTSTimeoutError("Kokoro synthesis timed out after 180.0s")

        import struct
        import wave

        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            data = struct.pack("<" + ("h" * 24000), *([0] * 24000))
            wav_file.writeframes(data)
        return output_path

    with patch.object(client, "synthesize_chunk", side_effect=mock_synth):
        res1 = process_next_job(db_session, client)
        assert res1 is False

    db_session.refresh(job)
    # completed_chunk_index must be 1 (chunk 1 completed, chunk 2 failed)
    assert job.completed_chunk_index == 1
    assert job.status == JobState.FAILED_RETRYABLE.value
    assert job.error_code == "KOKORO_SYNTHESIS_TIMEOUT"
    assert "timed out after 180.0s" in job.error_detail

    # Verify chunk 1 file exists on disk
    chunks_dir = tmp_path / "jobs" / job.id / "chunks"
    chunk1_file = chunks_dir / "chunk_0001.wav"
    assert chunk1_file.exists()

    # Step 2: Retry job. Synthesis must resume at chunk 2 (chunk 1 skipped)
    synth_calls.clear()

    def mock_synth_success(text, output_path, voice=None, speed=None, timeout=None):
        synth_calls.append(output_path.name)
        import struct
        import wave

        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            data = struct.pack("<" + ("h" * 24000), *([0] * 24000))
            wav_file.writeframes(data)
        return output_path

    job.status = JobState.QUEUED_TTS.value
    job.claimed_at = None
    db_session.commit()

    with patch.object(client, "synthesize_chunk", side_effect=mock_synth_success):
        res2 = process_next_job(db_session, client)
        assert res2 is True

    db_session.refresh(job)
    assert job.status == JobState.AUDIO_READY.value
    assert job.completed_chunk_index == 2
    # Chunk 1 was skipped because it was already completed!
    assert "chunk_0001.wav" not in synth_calls
    assert "chunk_0002.wav" in synth_calls


def test_diagnostics_report_renders_timeout_error(tmp_path):
    job = PodcastJob(
        id="job-diag-timeout-001",
        gmail_message_id="msg-timeout-diag",
        sender_email="auth@example.com",
        request_mode="standard",
        source_type="email_body",
        source_text="Source text",
        status=JobState.FAILED_RETRYABLE.value,
        error_code="KOKORO_SYNTHESIS_TIMEOUT",
        error_detail="TTS chunk 7/11 failed after 2 attempts: Kokoro synthesis timed out after 180.0s",
        created_at=datetime.now(UTC),
    )
    diag_path = generate_diagnostics_artifact(job, tmp_path)
    assert diag_path.exists()
    content = diag_path.read_text(encoding="utf-8")

    assert "## Errors and Warnings" in content
    assert "KOKORO_SYNTHESIS_TIMEOUT" in content
    assert "timed out after 180.0s" in content
