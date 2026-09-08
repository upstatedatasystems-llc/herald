"""
Unit tests for settings_fingerprint service.
Verifies immutable snapshot persistence, fallback to legacy columns, comparison logic,
and human-readable descriptions.
"""

from herald.db.models import PodcastJob
from herald.services.settings_fingerprint import (
    are_generation_settings_identical,
    build_generation_settings_snapshot,
    format_settings_display,
    get_job_generation_settings,
)


def test_build_and_get_job_generation_settings():
    snap = build_generation_settings_snapshot(
        mode="research",
        research_depth="high",
        voice="af_bella",
        speed=1.1,
        custom_title="Custom Ep",
        chunk_chars=400,
        verify=True,
    )
    assert snap["mode"] == "research"
    assert snap["research_depth"] == "high"
    assert snap["voice"] == "af_bella"
    assert snap["speed"] == 1.1

    job = PodcastJob(
        id="job-snap-1",
        generation_settings_json=snap,
        request_mode="standard",  # Should be overridden by immutable snapshot
    )
    retrieved = get_job_generation_settings(job)
    assert retrieved["mode"] == "research"
    assert retrieved["research_depth"] == "high"
    assert retrieved["voice"] == "af_bella"
    assert retrieved["speed"] == 1.1


def test_legacy_job_fallback():
    legacy_job = PodcastJob(
        id="job-legacy-1",
        generation_settings_json=None,
        request_mode="brief",
        custom_voice="am_adam",
        custom_speed=0.9,
    )
    retrieved = get_job_generation_settings(legacy_job)
    assert retrieved["mode"] == "brief"
    assert retrieved["voice"] == "am_adam"
    assert retrieved["speed"] == 0.9


def test_are_generation_settings_identical():
    base = {
        "mode": "standard",
        "voice": "af_heart",
        "speed": 1.0,
    }
    same = {
        "mode": "standard",
        "voice": "af_heart",
        "speed": 1.0,
    }
    diff_mode = {
        "mode": "brief",
        "voice": "af_heart",
        "speed": 1.0,
    }
    diff_voice = {
        "mode": "standard",
        "voice": "af_bella",
        "speed": 1.0,
    }
    diff_speed = {
        "mode": "standard",
        "voice": "af_heart",
        "speed": 1.1,
    }

    assert are_generation_settings_identical(base, same) is True
    assert are_generation_settings_identical(base, diff_mode) is False
    assert are_generation_settings_identical(base, diff_voice) is False
    assert are_generation_settings_identical(base, diff_speed) is False


def test_research_depth_comparison():
    res_med1 = {"mode": "research", "research_depth": "medium", "voice": "af_heart", "speed": 1.0}
    res_med2 = {"mode": "research", "research_depth": "medium", "voice": "af_heart", "speed": 1.0}
    res_high = {"mode": "research", "research_depth": "high", "voice": "af_heart", "speed": 1.0}

    assert are_generation_settings_identical(res_med1, res_med2) is True
    assert are_generation_settings_identical(res_med1, res_high) is False


def test_format_settings_display():
    assert format_settings_display({"mode": "standard", "voice": "af_heart", "speed": 1.0}) == "Standard • af_heart"
    assert format_settings_display({"mode": "standard", "voice": "af_heart", "speed": 1.1}) == "Standard • af_heart @ 1.1x"
    assert format_settings_display({"mode": "research", "research_depth": "high", "voice": "af_bella", "speed": 1.0}) == "Research (High) • af_bella"
    assert format_settings_display({"mode": "literal", "voice": "am_michael", "speed": 0.8}) == "Literal • am_michael @ 0.8x"
