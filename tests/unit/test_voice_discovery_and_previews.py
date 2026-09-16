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
