from herald.concurrency import (
    get_effective_tts_global_slots,
    resolve_concurrency_settings,
    tts_slot_lock,
)
from herald.config import settings


def test_slot_count_resolution_auto_profile(monkeypatch):
    """Auto concurrency profile with None slot override resolves to >= 1 valid integer."""
    monkeypatch.setattr(settings, "HERALD_CONCURRENCY_PROFILE", "auto")
    monkeypatch.setattr(settings, "HERALD_TTS_GLOBAL_SLOTS", None)

    slots = get_effective_tts_global_slots()
    assert isinstance(slots, int)
    assert slots >= 1


def test_slot_count_resolution_explicit_override(monkeypatch):
    """Explicit HERALD_TTS_GLOBAL_SLOTS override is honored."""
    monkeypatch.setattr(settings, "HERALD_CONCURRENCY_PROFILE", "auto")
    monkeypatch.setattr(settings, "HERALD_TTS_GLOBAL_SLOTS", 5)

    slots = get_effective_tts_global_slots()
    assert slots == 5


def test_slot_count_resolution_single_and_multi_slot_profiles(monkeypatch):
    """Test resolution across single-slot (minimal) and multi-slot (standard/performance) profiles."""
    # Minimal profile (single slot)
    monkeypatch.setattr(settings, "HERALD_CONCURRENCY_PROFILE", "minimal")
    monkeypatch.setattr(settings, "HERALD_TTS_GLOBAL_SLOTS", None)
    cfg_min = resolve_concurrency_settings("minimal")
    assert cfg_min.tts_global_slots >= 1

    # Standard profile (multi slot)
    cfg_std = resolve_concurrency_settings("standard")
    assert cfg_std.tts_global_slots >= 2

    # Performance profile (multi slot)
    cfg_perf = resolve_concurrency_settings("performance")
    assert cfg_perf.tts_global_slots >= 3


def test_local_semaphore_tts_slot_lock_concurrency():
    """Test local semaphore fallback when db=None."""
    with tts_slot_lock(db=None) as slot:
        assert slot is None
