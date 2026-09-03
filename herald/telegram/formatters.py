"""
Centralized HTML message formatters for Telegram interface.
All dynamic text values MUST be escaped with html.escape when used in HTML parse_mode.
"""

import html
from typing import Any

from herald.config import settings
from herald.db.models import PodcastJob, RequestMode
from herald.services.eta_calculator import calculate_script_duration


def get_job_ai_identity(job: PodcastJob) -> tuple[str | None, str | None]:
    """
    Return truthful (provider_name, model_name) for a job based on its request mode and persisted model evidence.
    Returns (None, None) for Literal mode (no AI used).
    """
    mode = getattr(job, "request_mode", RequestMode.STANDARD.value)
    if mode == RequestMode.LITERAL.value:
        return None, None

    if mode == RequestMode.RESEARCH.value:
        model = getattr(job, "research_model", None) or getattr(settings, "GEMINI_RESEARCH_MODEL", "gemini-2.5-flash")
        return "Gemini", model

    # Brief / Standard
    model = getattr(job, "gemini_model", None) or getattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    return "Gemini", model


def format_duration_sec(seconds: int | float | None) -> str:
    """Format seconds into human-readable duration (e.g. '3m 45s' or '45s')."""
    if seconds is None or seconds <= 0:
        return "0s"
    s = int(round(seconds))
    mins, sec = divmod(s, 60)
    if mins > 0:
        return f"{mins}m {sec}s"
    return f"{sec}s"


def format_quickstart(owner_name: str, default_mode: str, ai_provider: str) -> str:
    """Format quick-start onboarding message sent after /pair or authenticated /start."""
    esc_owner = html.escape(owner_name or "Owner")
    esc_mode = html.escape(default_mode or "standard")
    esc_ai = html.escape(ai_provider or "None (Literal only)")

    return (
        f"🎙️ <b>Welcome to Herald!</b>\n\n"
        f"Owner: <b>{esc_owner}</b>\n"
        f"Default Mode: <code>{esc_mode}</code>\n"
        f"AI Provider: <code>{esc_ai}</code>\n\n"
        f"<b>Quick Start:</b>\n"
        f"• Send an article URL for a <code>{esc_mode}</code> podcast.\n"
        f"• Put <code>brief</code> above a URL or text for a short episode.\n"
        f"• Put <code>research high</code> above a URL or text for deep research.\n"
        f"• Put <code>literal</code> above text for zero-AI narration.\n\n"
        f"Use /help to view all available commands and directives."
    )


def format_help() -> str:
    """Format comprehensive help and command reference message."""
    return (
        "📖 <b>Herald Usage Guide</b>\n\n"
        "<b>Ways to generate audio:</b>\n"
        "• Send an article URL (e.g. <code>https://example.com/article</code>)\n"
        "• Paste or forward an article, document, or newsletter\n\n"
        "<b>Modes (top of message):</b>\n"
        "• <code>literal</code> — Local deterministic reading (no AI required)\n"
        "• <code>brief</code> — Concise AI summary\n"
        "• <code>standard</code> — Full AI podcast narration\n"
        "• <code>research high</code> — Deep-dive grounded research podcast (Gemini)\n\n"
        "<b>Directives (top of message):</b>\n"
        "• <code>Voice: af_bella</code> (af_heart, af_bella, af_sarah, am_adam, am_michael)\n"
        "• <code>Speed: 1.1</code> (0.8 to 1.2)\n"
        "• <code>Title: Custom Title</code>\n\n"
        "<b>Commands:</b>\n"
        "/start — Quick-start guide\n"
        "/help — Full usage and directive reference\n"
        "/voices — Browse voices, hear samples, and set default voice\n"
        "/download — Download latest (or specific) episode MP3 file\n"
        "/status — Live system health, AI status, and queue depth\n"
        "/ai_check — Fresh AI provider connection test (alias: /ai-check)\n"
        "/queue — Pending and processing jobs\n"
        "/settings — Preferences and pre-TTS confirmation toggle\n"
        "/readme — Project documentation"
    )


def format_settings(user_prefs: dict, instance_settings: object = None) -> tuple[str, dict]:
    """
    Format settings message and generate inline keyboard markup for confirmation toggle.
    Returns:
        (text, reply_markup_dict)
    """
    confirm_on = bool(user_prefs.get("confirm_before_tts", False))
    default_voice = html.escape(str(user_prefs.get("default_voice", "af_heart")))
    default_speed = float(user_prefs.get("default_speed", 1.0))
    default_mode = html.escape(str(user_prefs.get("default_mode", "standard")).capitalize())
    ai_provider = html.escape(str(user_prefs.get("ai_provider", "None (Literal only)")))

    confirm_str = "🟢 On" if confirm_on else "⚪ Off"
    button_text = "🔕 Disable Confirm Before TTS" if confirm_on else "🔔 Enable Confirm Before TTS"
    button_callback = "h2:settings:confirm:off" if confirm_on else "h2:settings:confirm:on"

    text = (
        "⚙️ <b>Herald Preferences & Settings</b>\n\n"
        f"• <b>Default Mode:</b> <code>{default_mode}</code>\n"
        f"• <b>Default Voice:</b> <code>{default_voice}</code>\n"
        f"• <b>Default Speed:</b> <code>{default_speed:.1f}x</code>\n"
        f"• <b>Confirm Before TTS:</b> {confirm_str}\n"
        f"• <b>AI Provider:</b> <code>{ai_provider}</code>\n\n"
        "<i>Tap below to toggle pre-TTS approval confirmation:</i>"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": button_text,
                    "callback_data": button_callback,
                }
            ]
        ]
    }

    return text, reply_markup


def format_approval(job: PodcastJob, script_json: dict | None, eta_info: dict | None = None) -> tuple[str, dict]:
    """
    Format pre-TTS approval card with actual job and script data.
    Returns:
        (text, reply_markup_dict)
    """
    script_obj = script_json or job.script_json or {}
    title = html.escape(script_obj.get("episode_title") or job.custom_title or "Herald Episode")
    desc = script_obj.get("episode_description") or ""
    desc_clean = html.escape(desc[:200] + "..." if len(desc) > 200 else desc)

    mode_str = html.escape((job.request_mode or "standard").capitalize())
    if job.request_mode == RequestMode.RESEARCH.value and job.research_depth:
        mode_str += f" ({html.escape(job.research_depth.capitalize())})"

    source_words = len((job.source_text or "").split())
    dur_data = calculate_script_duration(script_obj, job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))
    narration_words = dur_data.get("narration_word_count", 0)
    pred_duration = format_duration_sec(dur_data.get("predicted_duration_seconds", 0))

    voice = html.escape(job.custom_voice or getattr(settings, "KOKORO_VOICE", "af_heart"))
    speed = float(job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))

    eta_range = (eta_info or {}).get("estimated_completion_range") or "approximately 3–5 minutes"
    short_id = html.escape(job.id[:8])

    ai_prov, ai_model = get_job_ai_identity(job)
    ai_line = f"\n• <b>AI Model:</b> <code>{html.escape(ai_prov)} ({html.escape(ai_model)})</code>" if ai_prov and ai_model else ""

    desc_section = f"\n<i>{desc_clean}</i>\n" if desc_clean else ""

    text = (
        f"📋 <b>Podcast Ready for Approval</b>\n\n"
        f"<b>{title}</b>{desc_section}\n"
        f"• <b>Mode:</b> {mode_str}\n"
        f"• <b>Source:</b> {source_words:,} words\n"
        f"• <b>Narration:</b> {narration_words:,} words (~{pred_duration})\n"
        f"• <b>Voice & Speed:</b> <code>{voice}</code> @ {speed:.1f}x"
        f"{ai_line}\n"
        f"• <b>Estimated Range:</b> {html.escape(eta_range)}\n"
        f"• <b>Job ID:</b> <code>{short_id}</code>\n\n"
        f"<i>Review details above and approve to start audio synthesis:</i>"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Approve & Generate",
                    "callback_data": f"h2:approve:{job.id}",
                },
                {
                    "text": "❌ Cancel",
                    "callback_data": f"h2:deny:{job.id}",
                },
            ]
        ]
    }

    return text, reply_markup


def format_queued(job: PodcastJob, script_json: dict | None, eta_info: dict | None = None) -> str:
    """Format rich queued card for automatically queued or explicitly approved jobs."""
    script_obj = script_json or job.script_json or {}
    title = html.escape(script_obj.get("episode_title") or job.custom_title or "Herald Episode")
    desc = script_obj.get("episode_description") or ""
    desc_clean = html.escape(desc[:200] + "..." if len(desc) > 200 else desc)

    mode_str = html.escape((job.request_mode or "standard").capitalize())
    if job.request_mode == RequestMode.RESEARCH.value and job.research_depth:
        mode_str += f" ({html.escape(job.research_depth.capitalize())})"

    source_words = len((job.source_text or "").split())
    dur_data = calculate_script_duration(script_obj, job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))
    narration_words = dur_data.get("narration_word_count", 0)
    pred_duration = format_duration_sec(dur_data.get("predicted_duration_seconds", 0))

    voice = html.escape(job.custom_voice or getattr(settings, "KOKORO_VOICE", "af_heart"))
    speed = float(job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))

    jobs_ahead = (eta_info or {}).get("jobs_ahead", 0)
    eta_range = (eta_info or {}).get("estimated_completion_range") or "approximately 3–5 minutes"
    short_id = html.escape(job.id[:8])

    ai_prov, ai_model = get_job_ai_identity(job)
    ai_line = f"\n• <b>AI Model:</b> <code>{html.escape(ai_prov)} ({html.escape(ai_model)})</code>" if ai_prov and ai_model else ""

    queue_line = f"\n• <b>Queue Position:</b> {jobs_ahead} jobs ahead" if jobs_ahead > 0 else "\n• <b>Queue Position:</b> Next up"
    desc_section = f"\n<i>{desc_clean}</i>\n" if desc_clean else ""

    return (
        f"🎙️ <b>Podcast Queued for Synthesis</b>\n\n"
        f"<b>{title}</b>{desc_section}\n"
        f"• <b>Mode:</b> {mode_str}\n"
        f"• <b>Source:</b> {source_words:,} words\n"
        f"• <b>Narration:</b> {narration_words:,} words (~{pred_duration})\n"
        f"• <b>Voice & Speed:</b> <code>{voice}</code> @ {speed:.1f}x"
        f"{ai_line}"
        f"{queue_line}\n"
        f"• <b>Estimated Range:</b> {html.escape(eta_range)}\n"
        f"• <b>Job ID:</b> <code>{short_id}</code>\n\n"
        f"Synthesizing audio now. You will receive the completed MP3 file shortly."
    )


def format_completion(
    job: PodcastJob,
    actual_chunks_count: int | None = None,
    file_size_bytes: int | None = None,
) -> str:
    """
    Format concise rich caption for audio delivery adhering to Telegram's 1024-char limit.
    """
    script_obj = job.script_json or {}
    title = html.escape(script_obj.get("episode_title") or job.custom_title or "Herald Episode")
    desc = script_obj.get("episode_description") or ""

    mode_str = html.escape((job.request_mode or "standard").capitalize())
    if job.request_mode == RequestMode.RESEARCH.value and job.research_depth:
        mode_str += f" ({html.escape(job.research_depth.capitalize())})"

    dur_str = format_duration_sec(job.audio_duration_seconds)
    size_mb_str = f"{file_size_bytes / (1024 * 1024):.1f} MB" if file_size_bytes else ""
    dur_size_str = f"{dur_str} ({size_mb_str})" if size_mb_str else dur_str

    # Active processing time calculation
    proc_time_str = ""
    if job.completed_at and job.created_at:
        total_sec = (job.completed_at - job.created_at).total_seconds()
        if job.approved_at and job.approval_requested_at:
            hold_sec = (job.approved_at - job.approval_requested_at).total_seconds()
            active_sec = max(1, int(total_sec - hold_sec))
        else:
            active_sec = max(1, int(total_sec))
        proc_time_str = format_duration_sec(active_sec)

    chunks_str = f"{actual_chunks_count} chunks" if actual_chunks_count else ""
    voice = html.escape(job.kokoro_voice or job.custom_voice or getattr(settings, "KOKORO_VOICE", "af_heart"))
    speed = float(job.kokoro_speed or job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))
    short_id = html.escape(job.id[:8])

    ai_prov, ai_model = get_job_ai_identity(job)
    ai_line = f"\n• <b>AI Model:</b> <code>{html.escape(ai_prov)} ({html.escape(ai_model)})</code>" if ai_prov and ai_model else ""

    # Truncate description safely to stay well within 1024 chars
    desc_clean = html.escape(desc[:150] + "..." if len(desc) > 150 else desc) if desc else ""
    desc_section = f"\n<i>{desc_clean}</i>\n" if desc_clean else ""

    chunks_line = f"\n• <b>TTS Chunks:</b> {chunks_str}" if chunks_str else ""
    proc_line = f"\n• <b>Processing Time:</b> {proc_time_str}" if proc_time_str else ""

    return (
        f"🎙️ <b>{title}</b>{desc_section}\n"
        f"• <b>Duration:</b> {dur_size_str}\n"
        f"• <b>Mode:</b> {mode_str}\n"
        f"• <b>Voice & Speed:</b> <code>{voice}</code> @ {speed:.1f}x"
        f"{ai_line}"
        f"{chunks_line}"
        f"{proc_line}\n"
        f"• <b>Job ID:</b> <code>{short_id}</code>"
    )


def format_completion_markup(job: PodcastJob) -> dict[str, Any]:
    """Return inline keyboard markup with Download MP3 button for completed podcasts."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📥 Download MP3",
                    "callback_data": f"h2:download:{job.id}",
                }
            ]
        ]
    }


def format_voices_browser(current_default: str) -> tuple[str, dict[str, Any]]:
    """
    Format interactive voice browser message and generate inline keyboard markup.
    Returns:
        (text, reply_markup_dict)
    """
    from herald.services.voice_manager import get_all_voice_metadata

    curr_clean = current_default.lower().strip()
    voices = get_all_voice_metadata()

    lines = [
        "🗣️ <b>Herald Voice Catalog</b>\n",
        "Select a voice below to preview a sample or set your default voice:\n",
    ]

    keyboard = []
    for meta in voices:
        vid = meta["voice_id"]
        dname = meta["display_name"]
        gender = meta["gender"]
        desc = meta["description"]
        is_curr = vid == curr_clean

        marker = " 🟢 <i>(Default)</i>" if is_curr else ""
        lines.append(f"• <b>{html.escape(dname)}</b> (<code>{html.escape(vid)}</code>) — <i>{html.escape(gender)}</i>{marker}\n  {html.escape(desc)}")

        btn_sample = {
            "text": f"🔊 Sample {dname}",
            "callback_data": f"h2:voice:sample:{vid}",
        }
        btn_set = {
            "text": "✅ Default" if is_curr else f"⭐ Set {dname}",
            "callback_data": f"h2:voice:set:{vid}",
        }
        keyboard.append([btn_sample, btn_set])

    lines.append("\n<i>Tip: You can also use <code>Voice: <name></code> at the top of any message.</i>")
    text = "\n".join(lines)
    reply_markup = {"inline_keyboard": keyboard}

    return text, reply_markup
