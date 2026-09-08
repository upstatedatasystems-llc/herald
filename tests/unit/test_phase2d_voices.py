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
    HERALD_VOICE_SAMPLE_CACHE_VERSION,
    VOICE_SAMPLE_TEXT,
    compute_sample_text_hash,
    convert_wav_to_mp3,
    ensure_voice_sample,
    is_valid_sample_audio,
    save_voice_sample_manifest,
)
from herald.telegram.auth import generate_pairing_code, verify_and_claim_pairing_code
from herald.telegram.bot import (
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


def test_voice_sample_uses_identical_comparison_phrase_across_all_voices(db_session, monkeypatch, tmp_path):
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


def test_voice_sample_callback_cache_miss_returns_unavailable_without_synthesis(
    db_session, monkeypatch, tmp_path
):
    """
    On cache miss, bot immediately responds with an unavailable alert.
    Zero Kokoro calls or runtime synthesis tasks are permitted.
    """
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr("herald.services.voice_manager.settings.HERALD_WORK_DIR", str(tmp_path))

    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)

    cb_query = {
        "id": "cb-miss-1",
        "from": {"id": 12345},
        "message": {"message_id": 701, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:voice:sample:af_bella",
    }

    with patch("herald.telegram.bot.get_cached_voice_sample", return_value=None) as mock_get_cached:
        handle_telegram_callback_query(db_session, mock_client, cb_query)

        mock_get_cached.assert_called_once_with("af_bella")
        mock_client.answer_callback_query.assert_called_once_with(
            "cb-miss-1",
            text="⚠️ Voice preview is unavailable on this Herald installation.\nRebuild the voice preview cache and try again.",
            show_alert=True,
        )
        assert not mock_client.send_audio.called


def test_voice_sample_callback_cache_hit_sends_audio(db_session, monkeypatch, tmp_path):
    """
    On cache hit, bot plays cached sample immediately.
    """
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr("herald.services.voice_manager.settings.HERALD_WORK_DIR", str(tmp_path))

    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)
    sample_file = tmp_path / "sample_af_heart.mp3"
    sample_file.write_bytes(b"dummy mp3 data")

    cb_query = {
        "id": "cb-hit-1",
        "from": {"id": 12345},
        "message": {"message_id": 702, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:voice:sample:af_heart",
    }

    with patch("herald.telegram.bot.get_cached_voice_sample", return_value=sample_file):
        handle_telegram_callback_query(db_session, mock_client, cb_query)

        mock_client.answer_callback_query.assert_called_once_with(
            "cb-hit-1",
            text="Playing sample for Heart...",
        )
        mock_client.send_audio.assert_called_once()
        call_kwargs = mock_client.send_audio.call_args[1]
        assert call_kwargs["audio_path"] == sample_file
        assert "Heart" in call_kwargs["title"]


def test_voice_browser_html_safety_escaping(monkeypatch):
    """
    Test that voice browser correctly escapes dynamic metadata containing <, >, &, quotes,
    and specifically that the tip format contains valid escaped HTML ('&lt;name&gt;' rather than '<name>').
    """
    from herald.telegram.formatters import format_voices_browser

    # Format standard catalog
    text, markup = format_voices_browser(current_default="af_heart")
    # Must NOT contain raw unescaped <name>
    assert "<name>" not in text
    assert "&lt;name&gt;" in text or "&lt;" in text

    # Test with custom tricky voice metadata
    fake_voices = [
        {
            "voice_id": "test_<voice>&1",
            "display_name": "Test <Voice> & Co \"Special\"",
            "gender": "Female <X>",
            "description": "A <bold> test voice with & special chars 'quotes' and <name>.",
        }
    ]
    monkeypatch.setattr("herald.services.voice_manager.get_all_voice_metadata", lambda: fake_voices)

    text_tricky, markup_tricky = format_voices_browser(current_default="test_<voice>&1")
    assert "<bold>" not in text_tricky
    assert "&lt;bold&gt;" in text_tricky
    assert "&lt;name&gt;" in text_tricky
    assert "&amp;" in text_tricky
    assert "&lt;Voice&gt;" in text_tricky
    assert "&lt;X&gt;" in text_tricky

    # Buttons should have Back to Settings and Selected
    keyboard = markup_tricky.get("inline_keyboard", [])
    assert any(b.get("text") == "← Back to Settings" for row in keyboard for b in row)
    assert any("✅ Selected" in b.get("text") for row in keyboard for b in row)


def test_settings_voice_navigation_and_selection_flow(db_session, monkeypatch):
    """
    Test full flow:
    1. /settings shows '🎙 Set Voice' button.
    2. Clicking '🎙 Set Voice' (h2:settings:voice) opens voice browser with '← Back to Settings'.
    3. Clicking a voice 'Use Bella' (h2:voice:set:af_bella) sets default voice.
    4. Clicking '← Back to Settings' (h2:settings:main) returns to settings showing 'af_bella'.
    """
    from herald.telegram.auth import get_effective_user_preferences

    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=99999, chat_id=99999, username="owner")

    mock_client = MagicMock(spec=TelegramClient)

    # 1. /settings command
    msg = {"chat": {"id": 99999, "type": "private"}, "from": {"id": 99999}, "message_id": 801}
    handle_telegram_command(db_session, mock_client, msg, "settings", "")
    assert mock_client.send_message.called
    sent_text = mock_client.send_message.call_args[1]["text"]
    sent_markup = mock_client.send_message.call_args[1]["reply_markup"]
    assert "Herald Preferences & Settings" in sent_text
    assert any(b.get("callback_data") == "h2:settings:voice" for row in sent_markup["inline_keyboard"] for b in row)

    # 2. Click '🎙 Set Voice' (h2:settings:voice)
    cb_voice = {
        "id": "cb-nav-1",
        "from": {"id": 99999},
        "message": {"message_id": 801, "chat": {"id": 99999, "type": "private"}},
        "data": "h2:settings:voice",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_voice)
    assert mock_client.edit_message_text.called
    edited_text = mock_client.edit_message_text.call_args[1]["text"]
    edited_markup = mock_client.edit_message_text.call_args[1]["reply_markup"]
    assert "Herald Voice Catalog" in edited_text
    assert any(b.get("callback_data") == "h2:settings:main" for row in edited_markup["inline_keyboard"] for b in row)

    # 3. Select 'af_bella' (h2:voice:set:af_bella)
    cb_set = {
        "id": "cb-nav-2",
        "from": {"id": 99999},
        "message": {"message_id": 801, "chat": {"id": 99999, "type": "private"}},
        "data": "h2:voice:set:af_bella",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_set)
    prefs = get_effective_user_preferences(db_session, 99999)
    assert prefs["default_voice"] == "af_bella"

    # 4. Click '← Back to Settings' (h2:settings:main)
    cb_back = {
        "id": "cb-nav-3",
        "from": {"id": 99999},
        "message": {"message_id": 801, "chat": {"id": 99999, "type": "private"}},
        "data": "h2:settings:main",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_back)
    assert mock_client.edit_message_text.called
    back_text = mock_client.edit_message_text.call_args[1]["text"]
    assert "Herald Preferences & Settings" in back_text
    assert "af_bella" in back_text


def test_legacy_voices_command_alias(db_session):
    """
    Test sending /voices manually still returns voice browser safely without crashing.
    """
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=88888, chat_id=88888, username="owner")

    mock_client = MagicMock(spec=TelegramClient)
    msg = {"chat": {"id": 88888, "type": "private"}, "from": {"id": 88888}, "message_id": 901}
    handle_telegram_command(db_session, mock_client, msg, "voices", "")

    assert mock_client.send_message.called
    sent_text = mock_client.send_message.call_args[1]["text"]
    sent_markup = mock_client.send_message.call_args[1]["reply_markup"]
    assert "Herald Voice Catalog" in sent_text
    assert any(b.get("callback_data") == "h2:settings:main" for row in sent_markup["inline_keyboard"] for b in row)


def test_voice_sample_audio_delivery_clean_and_media_callback_safe(db_session, monkeypatch, tmp_path):
    """
    Test that:
    1. Sample delivery does not attach redundant inline keyboards to audio messages.
    2. Any callback originating from a media/audio message updates voice preferences safely without calling editMessageText.
    """
    monkeypatch.setenv("HERALD_MOCK_TTS", "1")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr("herald.services.voice_manager.settings.HERALD_WORK_DIR", str(tmp_path))

    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=77777, chat_id=77777, username="owner")

    # Seed sample audio (filename is sample_<voice>.mp3) and manifest
    sample_file = tmp_path / "voice_samples" / "sample_af_sarah.mp3"
    sample_file.parent.mkdir(parents=True, exist_ok=True)
    sample_file.write_bytes(b"dummy audio data for sample")
    save_voice_sample_manifest({
        "af_sarah": {
            "voice_id": "af_sarah",
            "sample_text_hash": compute_sample_text_hash(),
            "speed": 1.0,
            "format": "mp3",
            "cache_version": HERALD_VOICE_SAMPLE_CACHE_VERSION,
            "file_path": str(sample_file),
        }
    })

    mock_client = MagicMock(spec=TelegramClient)

    # 1. Trigger sample delivery
    cb_sample = {
        "id": "cb-sample-1",
        "from": {"id": 77777},
        "message": {"message_id": 950, "chat": {"id": 77777, "type": "private"}, "text": "Catalog"},
        "data": "h2:voice:sample:af_sarah",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_sample)

    assert mock_client.send_audio.called
    audio_kwargs = mock_client.send_audio.call_args[1]
    assert audio_kwargs.get("reply_markup") is None  # Clean audio delivery without inline buttons

    # 2. Callback from media/audio message (e.g. without 'text' field)
    cb_media_set = {
        "id": "cb-sample-2",
        "from": {"id": 77777},
        "message": {
            "message_id": 951,
            "chat": {"id": 77777, "type": "private"},
            "audio": {"file_id": "aud123"},  # Non-text audio message
        },
        "data": "h2:voice:set:af_sarah",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_media_set)

    # Must set preference and answer callback query
    assert mock_client.answer_callback_query.called
    from herald.telegram.auth import get_effective_user_preferences
    prefs = get_effective_user_preferences(db_session, 77777)
    assert prefs["default_voice"] == "af_sarah"
    # edit_message_text must NOT be called on non-text audio message!
    assert not mock_client.edit_message_text.called


def test_get_cached_voice_sample_rejects_orphans_and_version_mismatch(monkeypatch, tmp_path):
    """
    get_cached_voice_sample must reject:
    1. Orphan files on disk without manifest entry
    2. Files with mismatched sample_text_hash
    3. Files with mismatched speed or format
    4. Files with mismatched cache_version
    """
    from herald.services.voice_manager import get_cached_voice_sample

    monkeypatch.setenv("HERALD_MOCK_TTS", "1")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr("herald.services.voice_manager.settings.HERALD_WORK_DIR", str(tmp_path))

    sample_file = tmp_path / "voice_samples" / "sample_af_bella.mp3"
    sample_file.parent.mkdir(parents=True, exist_ok=True)
    sample_file.write_bytes(b"dummy mp3 data")

    # 1. Orphan file (no manifest at all)
    assert get_cached_voice_sample("af_bella") is None

    # 2. Manifest with wrong version
    save_voice_sample_manifest({
        "af_bella": {
            "voice_id": "af_bella",
            "sample_text_hash": compute_sample_text_hash(),
            "speed": 1.0,
            "format": "mp3",
            "cache_version": "v0_legacy",
            "file_path": str(sample_file),
        }
    })
    assert get_cached_voice_sample("af_bella") is None

    # 3. Manifest with wrong text hash
    save_voice_sample_manifest({
        "af_bella": {
            "voice_id": "af_bella",
            "sample_text_hash": "different_hash",
            "speed": 1.0,
            "format": "mp3",
            "cache_version": HERALD_VOICE_SAMPLE_CACHE_VERSION,
            "file_path": str(sample_file),
        }
    })
    assert get_cached_voice_sample("af_bella") is None

    # 4. Manifest with wrong speed
    save_voice_sample_manifest({
        "af_bella": {
            "voice_id": "af_bella",
            "sample_text_hash": compute_sample_text_hash(),
            "speed": 1.5,
            "format": "mp3",
            "cache_version": HERALD_VOICE_SAMPLE_CACHE_VERSION,
            "file_path": str(sample_file),
        }
    })
    assert get_cached_voice_sample("af_bella") is None

    # 5. Matching manifest -> successfully cached
    save_voice_sample_manifest({
        "af_bella": {
            "voice_id": "af_bella",
            "sample_text_hash": compute_sample_text_hash(),
            "speed": 1.0,
            "format": "mp3",
            "cache_version": HERALD_VOICE_SAMPLE_CACHE_VERSION,
            "file_path": str(sample_file),
        }
    })
    assert get_cached_voice_sample("af_bella") == sample_file
