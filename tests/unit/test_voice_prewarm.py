from unittest.mock import MagicMock, patch

import pytest

from herald.config import settings
from herald.services.voice_manager import (
    HERALD_VOICE_SAMPLE_CACHE_VERSION,
    compute_sample_text_hash,
    ensure_voice_sample,
    get_cached_voice_sample,
    load_voice_sample_manifest,
    prewarm_all_voice_samples,
    save_voice_sample_manifest,
)
from herald.telegram.bot import handle_telegram_callback_query
from herald.telegram.client import TelegramClient


@pytest.fixture(autouse=True)
def mock_tts_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERALD_MOCK_TTS", "1")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    return tmp_path


def test_manifest_lifecycle():
    manifest_data = {
        "af_heart": {
            "voice_id": "af_heart",
            "sample_text_hash": compute_sample_text_hash(),
            "text_hash": compute_sample_text_hash(),
            "speed": 1.0,
            "format": "mp3",
            "cache_version": HERALD_VOICE_SAMPLE_CACHE_VERSION,
            "file_path": "/tmp/test.mp3",
        }
    }
    save_voice_sample_manifest(manifest_data)

    loaded = load_voice_sample_manifest()
    assert "af_heart" in loaded
    assert loaded["af_heart"]["sample_text_hash"] == compute_sample_text_hash()
    assert loaded["af_heart"]["cache_version"] == HERALD_VOICE_SAMPLE_CACHE_VERSION


def test_get_cached_voice_sample_miss_and_hit():
    # Cache miss initially
    assert get_cached_voice_sample("af_heart") is None

    # Prewarm af_heart
    path = ensure_voice_sample("af_heart")
    assert path.exists()

    # Now cache hit
    cached = get_cached_voice_sample("af_heart")
    assert cached is not None
    assert cached == path


def test_prewarm_all_voice_samples():
    results = prewarm_all_voice_samples()
    allowed = settings.get_allowed_voices_list()

    for v in allowed:
        assert results.get(v) is True
        sample = get_cached_voice_sample(v)
        assert sample is not None
        assert sample.exists()

    manifest = load_voice_sample_manifest()
    for v in allowed:
        assert v in manifest
        assert manifest[v]["text_hash"] == compute_sample_text_hash()


def test_telegram_callback_fast_serving():
    # Prewarm af_bella
    sample_path = ensure_voice_sample("af_bella")
    assert sample_path.exists()

    mock_client = MagicMock(spec=TelegramClient)
    cb_query = {
        "id": "cb-voice-1",
        "data": "h2:voice:sample:af_bella",
        "from": {"id": 12345},
        "message": {"message_id": 999, "chat": {"id": 12345, "type": "private"}},
    }

    mock_db = MagicMock()
    with patch("herald.telegram.bot.is_user_authorized", return_value=True):
        handle_telegram_callback_query(mock_db, mock_client, cb_query)

    mock_client.send_audio.assert_called_once()
    call_args = mock_client.send_audio.call_args[1]
    assert call_args["chat_id"] == 12345
    assert call_args["audio_path"] == sample_path
    assert "af_bella" in call_args["caption"]
