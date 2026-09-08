"""
Generation settings snapshot and comparison service for Herald.
Provides immutable snapshots of request parameters at job creation time,
canonical fingerprinting, and truthful user-facing comparison formatting.
"""

from typing import Any

from herald.config import settings
from herald.db.models import PodcastJob, RequestMode


def build_generation_settings_snapshot(
    mode: str,
    research_depth: str | None = None,
    voice: str | None = None,
    speed: float | None = None,
    custom_title: str | None = None,
    chunk_chars: int | None = None,
    verify: bool | None = None,
    ai_provider: str | None = None,
) -> dict[str, Any]:
    """Build canonical immutable generation settings snapshot dictionary."""
    clean_mode = (mode or RequestMode.STANDARD.value).lower().strip()
    eff_voice = (voice or getattr(settings, "KOKORO_VOICE", "af_heart")).strip()
    eff_speed = round(float(speed if speed is not None else getattr(settings, "KOKORO_SPEED", 1.0)), 2)
    clean_depth = (research_depth or "").lower().strip() if clean_mode == RequestMode.RESEARCH.value else None

    return {
        "mode": clean_mode,
        "research_depth": clean_depth,
        "voice": eff_voice,
        "speed": eff_speed,
        "custom_title": (custom_title or "").strip() or None,
        "chunk_chars": chunk_chars or getattr(settings, "TTS_CHUNK_DEFAULT_CHARS", 500),
        "verify": bool(verify),
        "ai_provider": (ai_provider or getattr(settings, "AI_PROVIDER", "gemini")).strip(),
    }


def get_job_generation_settings(job: PodcastJob) -> dict[str, Any]:
    """
    Retrieve generation settings for a job.
    Uses immutable generation_settings_json snapshot if present;
    falls back cleanly to individual historical job columns for legacy rows.
    """
    if job.generation_settings_json and isinstance(job.generation_settings_json, dict):
        return dict(job.generation_settings_json)

    # Legacy fallback from historical row columns
    mode = (job.request_mode or RequestMode.STANDARD.value).lower().strip()
    voice = (job.custom_voice or job.kokoro_voice or getattr(settings, "KOKORO_VOICE", "af_heart")).strip()
    speed = round(
        float(job.custom_speed or job.kokoro_speed or getattr(settings, "KOKORO_SPEED", 1.0)),
        2,
    )
    depth = (job.research_depth or "").lower().strip() if mode == RequestMode.RESEARCH.value else None

    return {
        "mode": mode,
        "research_depth": depth,
        "voice": voice,
        "speed": speed,
        "custom_title": job.custom_title,
        "chunk_chars": job.tts_chunk_chars or 500,
        "verify": bool(job.verify_final_script),
        "ai_provider": getattr(settings, "AI_PROVIDER", "gemini"),
    }


def are_generation_settings_identical(prior: dict[str, Any], current: dict[str, Any]) -> bool:
    """
    Compare generation-affecting settings between a prior job and a new request.
    Evaluates mode, research depth (for research mode), voice, and speed.
    Returns True if generation output would use identical settings, False otherwise.
    """
    m_prior = (prior.get("mode") or "").lower().strip()
    m_curr = (current.get("mode") or "").lower().strip()
    if m_prior != m_curr:
        return False

    if m_curr == RequestMode.RESEARCH.value:
        d_prior = (prior.get("research_depth") or "medium").lower().strip()
        d_curr = (current.get("research_depth") or "medium").lower().strip()
        if d_prior != d_curr:
            return False

    v_prior = (prior.get("voice") or getattr(settings, "KOKORO_VOICE", "af_heart")).lower().strip()
    v_curr = (current.get("voice") or getattr(settings, "KOKORO_VOICE", "af_heart")).lower().strip()
    if v_prior != v_curr:
        return False

    s_prior = round(float(prior.get("speed") or 1.0), 2)
    s_curr = round(float(current.get("speed") or 1.0), 2)
    if s_prior != s_curr:
        return False

    return True


def format_settings_display(settings_dict: dict[str, Any]) -> str:
    """
    Format a concise, human-readable description of generation settings.
    Examples:
        Standard • af_heart
        Standard • af_heart @ 1.1x
        Research (High) • af_bella
        Literal • am_adam @ 0.9x
    """
    mode = (settings_dict.get("mode") or "standard").capitalize()
    depth = settings_dict.get("research_depth")
    if (settings_dict.get("mode") or "").lower() == RequestMode.RESEARCH.value and depth:
        mode = f"Research ({depth.capitalize()})"

    voice = settings_dict.get("voice") or getattr(settings, "KOKORO_VOICE", "af_heart")
    speed = round(float(settings_dict.get("speed") or 1.0), 2)

    if abs(speed - 1.0) >= 0.05:
        voice_str = f"{voice} @ {speed:.1f}x"
    else:
        voice_str = voice

    return f"{mode} • {voice_str}"
