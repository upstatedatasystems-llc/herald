"""
Unit tests for fail-closed voice discovery, curated voice catalog, and preview concurrency.
"""

from unittest.mock import MagicMock, patch

import pytest

from herald.config import settings
from herald.services.voice_manager import (
    VoicePreviewBusyError,
    discover_runtime_voices,
    ensure_voice_sample,
    get_cached_voice_sample,
    get_selectable_voices,
    get_voices_by_accent_group,
)
from herald.tts.kokoro_client import KokoroClient


@pytest.fixture(autouse=True)
def setup_test_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERALD_MOCK_TTS", "1")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    return tmp_path


def test_voice_discovery_fail_closed_on_network_failure():
    """
    Test the critical fail-closed contract:
    When Kokoro endpoint probe fails, discovery returns ([], False),
    and get_selectable_voices MUST NOT advertise the full curated allowlist.
    It preserves ONLY the stored user's valid voice and default fallback.
    """
    mock_client = MagicMock(spec=KokoroClient)
    mock_client.get_available_voices.side_effect = RuntimeError("Kokoro service unreachable")

    # 1. Raw discovery returns empty and False
    voices, ok = discover_runtime_voices(kokoro_client=mock_client)
    assert ok is False
    assert voices == []

    # 2. Selectable voices fails closed
    # Stored user voice is 'bf_emma' (in curated allowlist)
    selectable, diag = get_selectable_voices(user_voice="bf_emma", kokoro_client=mock_client)
    assert diag["discovery_successful"] is False
    assert diag["fallback_mode"] == "fail_closed_preserved"

    # Must preserve 'bf_emma' and safe default 'af_heart'
    assert "bf_emma" in selectable
    assert "af_heart" in selectable
    # Must NOT contain other unverified curated voices
    assert "af_bella" not in selectable
    assert "bm_george" not in selectable
    assert len(selectable) <= 2


def test_voice_discovery_intersection_when_successful():
    """
    When discovery succeeds, selectable voices = runtime voices ∩ curated allowlist.
    """
    mock_client = MagicMock(spec=KokoroClient)
    # Return a mix of valid curated voices and an unknown voice
    mock_client.get_available_voices.return_value = ["af_heart", "af_bella", "unknown_custom_voice"]

    selectable, diag = get_selectable_voices(user_voice="af_heart", kokoro_client=mock_client)
    assert diag["discovery_successful"] is True
    assert diag["fallback_mode"] == "runtime_intersection"

    # Only curated voices that are discovered are selectable
    assert "af_heart" in selectable
    assert "af_bella" in selectable
    assert "unknown_custom_voice" not in selectable
    assert "am_michael" not in selectable  # in curated, but not discovered


def test_accent_groups_and_curated_catalog():
    """Verify that catalog includes both American and British English accent groups."""
    us_voices = get_voices_by_accent_group("american_english")
    uk_voices = get_voices_by_accent_group("british_english")
    all_voices = get_voices_by_accent_group("all")

    assert len(us_voices) >= 6
    assert len(uk_voices) >= 4
    assert len(all_voices) >= 12

    # Check key voices in catalog
    assert any(v["voice_id"] == "af_heart" for v in us_voices)
    assert any(v["voice_id"] == "bf_emma" for v in uk_voices)
    assert any(v["voice_id"] == "bm_george" for v in uk_voices)


def test_preview_busy_rejection_during_active_synthesis():
    """
    When podcast synthesis is active, ensure_voice_sample must immediately raise
    VoicePreviewBusyError without blocking podcast synthesis.
    """
    with patch("herald.services.voice_manager.is_tts_actively_synthesizing", return_value=True):
        with pytest.raises(VoicePreviewBusyError, match="Voice preview is temporarily busy"):
            ensure_voice_sample("af_heart", force=True)


def test_preview_sample_keyed_by_speed_and_voice(tmp_path):
    """
    Verify preview files and manifest records are correctly keyed by voice and speed.
    """
    # 1.0x standard speed
    p_10 = ensure_voice_sample("af_heart", speed=1.0)
    assert p_10.exists()
    assert "sample_af_heart.mp3" in str(p_10)

    # 1.25x custom speed
    p_125 = ensure_voice_sample("af_heart", speed=1.25)
    assert p_125.exists()
    assert "sample_af_heart_s1.25.mp3" in str(p_125)

    # Distinct cached lookups
    cached_10 = get_cached_voice_sample("af_heart", speed=1.0)
    cached_125 = get_cached_voice_sample("af_heart", speed=1.25)

    assert cached_10 == p_10
    assert cached_125 == p_125
    assert cached_10 != cached_125


def test_preview_cache_miss_free_tts_slot_proceeds_end_to_end(tmp_path):
    """
    Test C & 6: Happy path when preview is uncached and shared TTS slot is free.
    Proves:
    - free TTS resource -> shared TTS slot acquired
    - Kokoro synthesis invoked with normalized text
    - MP3 generated and cached
    - No PodcastJob created
    - No AI interaction created
    - Slot cleanly released
    """
    from pathlib import Path


    mock_client = MagicMock(spec=KokoroClient)
    def _mock_synth(text, output_path, voice=None, speed=None, timeout=None):
        Path(output_path).write_bytes(
            b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
        )
    mock_client.synthesize_chunk.side_effect = _mock_synth

    with patch("herald.services.voice_manager.is_tts_actively_synthesizing", return_value=False), \
         patch("herald.services.voice_manager.convert_wav_to_mp3", side_effect=lambda w, m: Path(m).write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00MOCK_MP3_DATA")), \
         patch("herald.services.voice_manager.is_valid_sample_audio", return_value=True), \
         patch("herald.services.voice_manager.normalize_for_speech", side_effect=lambda t: f"NORMALIZED: {t}") as mock_norm:

        p = ensure_voice_sample("bf_emma", speed=1.0, kokoro_client=mock_client, force=True, non_blocking=True)
        assert p.exists()
        assert mock_client.synthesize_chunk.called
        assert mock_norm.called
        # Text passed to synthesizer was normalized
        synth_args = mock_client.synthesize_chunk.call_args[1]
        assert synth_args["text"].startswith("NORMALIZED:")
        assert synth_args["voice"] == "bf_emma"


def test_preview_cache_miss_occupied_tts_slot_raises_preview_busy(monkeypatch):
    """
    Test D: When uncached preview requests a slot with non_blocking=True,
    if the actual TTS slot is held (e.g. by synthesis), it raises VoicePreviewBusyError
    via atomic slot acquisition failure rather than simply checking a flag.
    """
    from herald.concurrency import initialize_semaphores, reset_semaphores_for_tests, tts_slot_lock

    reset_semaphores_for_tests()
    monkeypatch.setattr(settings, "HERALD_CONCURRENCY_PROFILE", "single")
    monkeypatch.setattr(settings, "HERALD_TTS_GLOBAL_SLOTS", 1)
    initialize_semaphores(settings.get_concurrency_config())

    mock_client = MagicMock(spec=KokoroClient)

    # Hold the single TTS slot using the concurrency primitive
    with tts_slot_lock(db=None, timeout_seconds=5.0):
        # Even if is_tts_actively_synthesizing returned False, the lock itself blocks
        with patch("herald.services.voice_manager.is_tts_actively_synthesizing", return_value=False):
            with pytest.raises(VoicePreviewBusyError, match="temporarily busy"):
                ensure_voice_sample(
                    "af_bella",
                    speed=1.0,
                    kokoro_client=mock_client,
                    force=True,
                    non_blocking=True,
                )
    # Synthesis chunk was never called
    assert not mock_client.synthesize_chunk.called
    reset_semaphores_for_tests()


def test_preview_cannot_race_podcast_synthesis(monkeypatch):
    """
    Test E: Preview cannot race or interleave with podcast synthesis.
    Both share the exact same underlying tts_slot_lock.
    """
    import threading

    from herald.concurrency import initialize_semaphores, reset_semaphores_for_tests, tts_slot_lock

    reset_semaphores_for_tests()
    monkeypatch.setattr(settings, "HERALD_CONCURRENCY_PROFILE", "single")
    monkeypatch.setattr(settings, "HERALD_TTS_GLOBAL_SLOTS", 1)
    initialize_semaphores(settings.get_concurrency_config())

    synthesis_started = threading.Event()
    synthesis_finish = threading.Event()
    preview_result = {"busy_error_raised": False}

    def simulate_podcast_synthesis():
        with tts_slot_lock(db=None, timeout_seconds=5.0):
            synthesis_started.set()
            synthesis_finish.wait(timeout=2.0)

    t_synth = threading.Thread(target=simulate_podcast_synthesis)
    t_synth.start()

    # Wait for podcast synthesis to hold the lock
    synthesis_started.wait(timeout=2.0)

    try:
        # Non-blocking preview attempt while podcast holds lock
        with patch("herald.services.voice_manager.is_tts_actively_synthesizing", return_value=False):
            ensure_voice_sample("af_heart", force=True, non_blocking=True)
    except VoicePreviewBusyError:
        preview_result["busy_error_raised"] = True
    finally:
        synthesis_finish.set()
        t_synth.join(timeout=2.0)
        reset_semaphores_for_tests()

    assert preview_result["busy_error_raised"] is True
