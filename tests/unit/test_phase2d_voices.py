import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import Base
from herald.services.voice_manager import (
    VOICE_SAMPLE_TEXT,
    convert_wav_to_mp3,
    ensure_voice_sample,
    is_valid_sample_audio,
)
from herald.telegram.auth import generate_pairing_code, verify_and_claim_pairing_code
from herald.telegram.bot import (
    _IN_FLIGHT_VOICE_SAMPLES,
    _VOICE_SAMPLE_LOCK,
    handle_telegram_callback_query,
    handle_telegram_command,
)
from herald.telegram.client import TelegramClient
from herald.tts.kokoro_client import KokoroClient


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_voice_sample_uses_identical_comparison_phrase_across_all_voices(
    db_session, monkeypatch, tmp_path
):
    """
    Every voice must synthesize exactly the same fixed comparison phrase.
    Voice names must NOT be injected into the spoken preview text.
    """
    monkeypatch.setenv("HERALD_MOCK_TTS", "1")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr("herald.services.voice_manager.settings.HERALD_WORK_DIR", str(tmp_path))

    mock_kokoro = MagicMock(spec=KokoroClient)
    synthesized_texts = []

    def mock_synth(text, output_path, voice=None, speed=None, timeout=None):
        synthesized_texts.append((voice, text))
        Path(output_path).write_bytes(b"dummy_wav_bytes")

    mock_kokoro.synthesize_chunk.side_effect = mock_synth

    voices_to_test = ["af_heart", "af_bella", "af_sarah", "am_adam", "am_michael"]
    for v in voices_to_test:
        ensure_voice_sample(voice=v, kokoro_client=mock_kokoro, db=db_session)

    assert len(synthesized_texts) == len(voices_to_test)
    for voice_name, text_used in synthesized_texts:
        assert text_used == VOICE_SAMPLE_TEXT
        assert voice_name not in text_used


def test_voice_sample_cache_validation_and_atomic_regeneration(db_session, monkeypatch, tmp_path):
    """
    Valid cache hits are reused. Corrupt/empty cache files are cleaned and regenerated.
    Temporary files are cleaned up in finally.
    """
    monkeypatch.setenv("HERALD_MOCK_TTS", "1")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr("herald.services.voice_manager.settings.HERALD_WORK_DIR", str(tmp_path))

    mock_kokoro = MagicMock(spec=KokoroClient)

    def mock_synth(text, output_path, voice=None, speed=None, timeout=None):
        Path(output_path).write_bytes(b"valid_wav_content_bytes")

    mock_kokoro.synthesize_chunk.side_effect = mock_synth

    # 1. Initial synthesis creates valid cache file
    p1 = ensure_voice_sample(voice="af_bella", kokoro_client=mock_kokoro, db=db_session)
    assert p1.exists()
    assert is_valid_sample_audio(p1)
    assert mock_kokoro.synthesize_chunk.call_count == 1

    # Verify no stray tmp files
    tmp_files = list(tmp_path.glob("voice_samples/*.tmp.*"))
    assert len(tmp_files) == 0

    # 2. Corrupt the file (truncate to 0 bytes)
    p1.write_bytes(b"")
    assert not is_valid_sample_audio(p1)

    # 3. Next call detects invalid cache, cleans it, and regenerates
    p2 = ensure_voice_sample(voice="af_bella", kokoro_client=mock_kokoro, db=db_session)
    assert p2.exists()
    assert is_valid_sample_audio(p2)
    assert mock_kokoro.synthesize_chunk.call_count == 2


def test_missing_ffmpeg_fails_in_production(monkeypatch, tmp_path):
    """
    In production (HERALD_MOCK_TTS!=1), missing FFmpeg raises RuntimeError and does not write fake MP3s.
    In explicit test/mock mode (HERALD_MOCK_TTS=1), mock MP3 is written.
    """
    dummy_wav = tmp_path / "test.wav"
    dummy_wav.write_bytes(b"wav_bytes")
    target_mp3 = tmp_path / "out.mp3"

    # Production mode without FFmpeg -> RuntimeError
    monkeypatch.delenv("HERALD_MOCK_TTS", raising=False)
    monkeypatch.setattr(shutil, "which", lambda x: None)

    with pytest.raises(RuntimeError, match="FFmpeg executable not found"):
        convert_wav_to_mp3(dummy_wav, target_mp3)

    assert not target_mp3.exists()

    # Explicit Mock TTS mode -> succeeds
    monkeypatch.setenv("HERALD_MOCK_TTS", "1")
    res = convert_wav_to_mp3(dummy_wav, target_mp3)
    assert res.exists()
    assert res.stat().st_size > 0


def test_voice_sample_callback_non_blocking_and_in_flight_dedup(db_session, monkeypatch, tmp_path):
    """
    Test that cache miss:
    1. Acknowledges callback promptly
    2. Submits background task and returns to polling loop immediately
    3. Allows processing other commands while sample is generating
    4. Deduplicates duplicate same-voice requests
    """
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr("herald.services.voice_manager.settings.HERALD_WORK_DIR", str(tmp_path))

    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)

    # Clean in-flight set before test
    with _VOICE_SAMPLE_LOCK:
        _IN_FLIGHT_VOICE_SAMPLES.clear()

    synth_started = False
    synth_can_finish = False

    def slow_ensure(voice, db=None, kokoro_client=None):
        nonlocal synth_started, synth_can_finish
        synth_started = True
        while not synth_can_finish:
            time.sleep(0.05)
        p = tmp_path / f"sample_{voice}.mp3"
        p.write_bytes(b"dummy mp3 data")
        return p

    cb_query = {
        "id": "cb-slow-1",
        "from": {"id": 12345},
        "message": {"message_id": 701, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:voice:sample:af_bella",
    }

    with patch("herald.telegram.bot.ensure_voice_sample", side_effect=slow_ensure):
        # 1. Trigger sample callback
        t0 = time.monotonic()
        handle_telegram_callback_query(db_session, mock_client, cb_query)
        elapsed = time.monotonic() - t0

        # Returning must be near-instantaneous (< 0.5s), NOT blocked on slow generation!
        assert elapsed < 0.5
        mock_client.answer_callback_query.assert_called_with(
            "cb-slow-1",
            text="Preparing voice sample for Bella... Herald will send it shortly.",
        )

        # 2. Process another command (e.g. /status) WHILE sample is generating in background
        status_msg = {
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "message_id": 702,
        }
        handle_telegram_command(db_session, mock_client, status_msg, "status", "")
        assert mock_client.send_message.called

        # 3. Duplicate same-voice click while in-flight -> prompt acknowledgment without launching 2nd task
        cb_dup = {
            "id": "cb-slow-dup",
            "from": {"id": 12345},
            "message": {"message_id": 703, "chat": {"id": 12345, "type": "private"}},
            "data": "h2:voice:sample:af_bella",
        }
        handle_telegram_callback_query(db_session, mock_client, cb_dup)
        mock_client.answer_callback_query.assert_called_with(
            "cb-slow-dup",
            text="Sample for Bella is already being prepared...",
            show_alert=False,
        )

        # Allow background task to complete
        synth_can_finish = True
        time.sleep(0.3)

    # Verify background task sent audio and cleared in-flight
    assert mock_client.send_audio.called
    with _VOICE_SAMPLE_LOCK:
        assert "af_bella" not in _IN_FLIGHT_VOICE_SAMPLES


def test_voice_sample_callback_generic_error_on_failure(db_session, monkeypatch, tmp_path):
    """
    On background synthesis failure, user receives a generic error without leaking raw exceptions.
    """
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr("herald.services.voice_manager.settings.HERALD_WORK_DIR", str(tmp_path))

    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)

    with _VOICE_SAMPLE_LOCK:
        _IN_FLIGHT_VOICE_SAMPLES.clear()

    cb_query = {
        "id": "cb-err-1",
        "from": {"id": 12345},
        "message": {"message_id": 704, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:voice:sample:af_sarah",
    }

    with patch(
        "herald.telegram.bot.ensure_voice_sample",
        side_effect=Exception("Internal secret db connection failed: /etc/passwd"),
    ):
        handle_telegram_callback_query(db_session, mock_client, cb_query)
        time.sleep(0.3)

    # Verify generic user message and in-flight cleanup
    mock_client.send_message.assert_called_once()
    sent_text = mock_client.send_message.call_args[1]["text"]
    assert "Voice sample generation failed. Please try again." in sent_text
    assert "/etc/passwd" not in sent_text
    assert "secret" not in sent_text

    with _VOICE_SAMPLE_LOCK:
        assert "af_sarah" not in _IN_FLIGHT_VOICE_SAMPLES
