"""
Centralized HTML message formatters for Telegram interface.
All dynamic text values MUST be escaped with html.escape when used in HTML parse_mode.
"""

import html
from datetime import UTC, datetime
from typing import Any

from herald.config import settings
from herald.db.models import PodcastJob, RequestMode
from herald.services.eta_calculator import calculate_script_duration


def get_job_ai_identity(job: PodcastJob) -> tuple[str | None, str | None]:
    """
    Return truthful (provider_name, model_name) for a job based on its request mode, active provider, and persisted model evidence.
    Returns (None, None) for Literal mode (no AI used).
    """
    mode = getattr(job, "request_mode", RequestMode.STANDARD.value)
    if mode == RequestMode.LITERAL.value:
        return None, None

    if mode == RequestMode.RESEARCH.value:
        model = getattr(job, "research_model", None) or getattr(settings, "GEMINI_RESEARCH_MODEL", "gemini-2.5-flash")
        return "Gemini", model

    # Check ai_interactions first for authoritative evidence
    if hasattr(job, "ai_interactions") and job.ai_interactions:
        first_ai = job.ai_interactions[0]
        return first_ai.provider.capitalize(), first_ai.model

    # Brief / Standard fallback to active AIProvider
    from herald.ai.factory import get_ai_provider

    prov = get_ai_provider()
    if prov:
        return prov.provider_name, getattr(job, "gemini_model", None) or prov.configured_model

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
        "/diagnostics — View job diagnostics and download support package\n"
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


def get_job_display_title(job: PodcastJob) -> str:
    """
    Return authoritative display title with strict precedence:
    job.custom_title -> (job.script_json or {}).get("episode_title") -> "Herald Episode"
    """
    if job.custom_title and str(job.custom_title).strip():
        return str(job.custom_title).strip()
    if job.script_json and isinstance(job.script_json, dict):
        ep_title = job.script_json.get("episode_title")
        if ep_title and str(ep_title).strip():
            return str(ep_title).strip()
    return "Herald Episode"


def format_approval(
    job: PodcastJob,
    script_json: dict | None,
    eta_info: dict | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Format interactive approval card message with Approve and Cancel buttons.
    Returns:
        (text, reply_markup_dict)
    """
    script_obj = script_json or job.script_json or {}
    title_raw = get_job_display_title(job)
    title = html.escape(title_raw[:100] + "..." if len(title_raw) > 100 else title_raw)
    desc = script_obj.get("episode_description") or ""
    desc_clean = html.escape(desc[:150] + "..." if len(desc) > 150 else desc)

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
    title_raw = get_job_display_title(job)
    title = html.escape(title_raw[:100] + "..." if len(title_raw) > 100 else title_raw)
    desc = script_obj.get("episode_description") or ""
    desc_clean = html.escape(desc[:150] + "..." if len(desc) > 150 else desc)

    mode_str = html.escape((job.request_mode or "standard").capitalize())
    if job.request_mode == RequestMode.RESEARCH.value and job.research_depth:
        mode_str += f" ({html.escape(job.research_depth.capitalize())})"

    source_words = len((job.source_text or "").split())
    dur_data = calculate_script_duration(script_obj, job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))
    narration_words = dur_data.get("narration_word_count", 0)
    pred_duration = format_duration_sec(dur_data.get("predicted_duration_seconds", 0))

    voice = html.escape(job.custom_voice or getattr(settings, "KOKORO_VOICE", "af_heart"))
    speed = float(job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))

    # Source type
    src_type_raw = job.source_type or "text"
    if job.source_url:
        src_disp = f"URL ({html.escape(job.source_url[:35])}{'...' if len(job.source_url) > 35 else ''})"
    elif src_type_raw == "email_body":
        src_disp = "Email body"
    else:
        src_disp = html.escape(src_type_raw.capitalize())

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
        f"• <b>Source:</b> {src_disp} ({source_words:,} words)\n"
        f"• <b>Narration:</b> {narration_words:,} words (~{pred_duration})\n"
        f"• <b>Voice & Speed:</b> <code>{voice}</code> @ {speed:.1f}x"
        f"{ai_line}"
        f"{queue_line}\n"
        f"• <b>Estimated Range:</b> {html.escape(eta_range)}\n"
        f"• <b>Job ID:</b> <code>{short_id}</code>\n\n"
        f"Queued for synthesis. Herald will begin when TTS capacity is available."
    )


def format_completion(
    job: PodcastJob,
    actual_chunks_count: int | None = None,
    file_size_bytes: int | None = None,
    active_processing_seconds: int | float | None = None,
) -> str:
    """
    Format concise rich caption for audio delivery adhering to Telegram's 1024-char limit.
    """
    script_obj = job.script_json or {}
    title_raw = get_job_display_title(job)
    title = html.escape(title_raw[:100] + "..." if len(title_raw) > 100 else title_raw)
    desc = script_obj.get("episode_description") or ""

    mode_str = html.escape((job.request_mode or "standard").capitalize())
    if job.request_mode == RequestMode.RESEARCH.value and job.research_depth:
        mode_str += f" ({html.escape(job.research_depth.capitalize())})"

    dur_str = format_duration_sec(job.audio_duration_seconds)
    size_mb_str = f"{file_size_bytes / (1024 * 1024):.1f} MB" if file_size_bytes else ""
    dur_size_str = f"{dur_str} ({size_mb_str})" if size_mb_str else dur_str

    # Word counts
    source_words = len((job.source_text or "").split()) if job.source_text else 0
    dur_data = calculate_script_duration(script_obj, job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))
    narration_words = dur_data.get("narration_word_count", 0)

    # Active processing time calculation
    proc_time_str = ""
    if active_processing_seconds is not None and active_processing_seconds > 0:
        proc_time_str = format_duration_sec(active_processing_seconds)
    elif job.completed_at and job.created_at:
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
    desc_clean = html.escape(desc[:120] + "..." if len(desc) > 120 else desc) if desc else ""
    desc_section = f"\n<i>{desc_clean}</i>\n" if desc_clean else ""

    words_line = ""
    if source_words > 0 and narration_words > 0:
        words_line = f"\n• <b>Words:</b> {source_words:,} src / {narration_words:,} nar"
    elif narration_words > 0:
        words_line = f"\n• <b>Words:</b> {narration_words:,} nar"

    chunks_line = f"\n• <b>TTS Chunks:</b> {chunks_str}" if chunks_str else ""
    proc_line = f"\n• <b>Processing Time:</b> {proc_time_str}" if proc_time_str else ""

    caption = (
        f"🎙️ <b>{title}</b>{desc_section}\n"
        f"• <b>Duration:</b> {dur_size_str}\n"
        f"• <b>Mode:</b> {mode_str}\n"
        f"• <b>Voice & Speed:</b> <code>{voice}</code> @ {speed:.1f}x"
        f"{ai_line}"
        f"{words_line}"
        f"{chunks_line}"
        f"{proc_line}\n"
        f"• <b>Job ID:</b> <code>{short_id}</code>"
    )

    # Ensure strictly within Telegram's 1024-char caption limit
    if len(caption) > 1024:
        # Emergency trim of description
        desc_section = ""
        caption = (
            f"🎙️ <b>{title}</b>\n"
            f"• <b>Duration:</b> {dur_size_str}\n"
            f"• <b>Mode:</b> {mode_str}\n"
            f"• <b>Voice & Speed:</b> <code>{voice}</code> @ {speed:.1f}x"
            f"{ai_line}"
            f"{words_line}"
            f"{chunks_line}"
            f"{proc_line}\n"
            f"• <b>Job ID:</b> <code>{short_id}</code>"
        )
    return caption


def format_completion_markup(job: PodcastJob) -> dict[str, Any]:
    """Return inline keyboard markup with Download MP3 and Diagnostics buttons for completed podcasts."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📥 Download MP3",
                    "callback_data": f"h2:download:{job.id}",
                },
                {
                    "text": "🛠️ Diagnostics",
                    "callback_data": f"h2:diag:{job.id}",
                },
            ]
        ]
    }


def format_diagnostics_card(job: PodcastJob, db: Any = None) -> str:
    """Format concise Telegram HTML diagnostic card for a job."""
    from herald.db.models import AIInteraction, PodcastTTSChunk

    title = html.escape(get_job_display_title(job))
    short_id = html.escape(job.id[:8])
    status = html.escape(job.status)
    mode_str = (job.request_mode or "standard").capitalize()
    if job.request_mode == "research" and job.research_depth:
        mode_str += f" ({job.research_depth.capitalize()})"
    mode_esc = html.escape(mode_str)
    src_type = html.escape(job.source_type or "text")

    voice = html.escape(job.kokoro_voice or job.custom_voice or getattr(settings, "KOKORO_VOICE", "af_heart"))
    speed = float(job.kokoro_speed or job.custom_speed or getattr(settings, "KOKORO_SPEED", 1.0))

    # Duration and processing time
    proc_time_str = "N/A"
    created_utc = job.created_at.replace(tzinfo=UTC) if (job.created_at and job.created_at.tzinfo is None) else job.created_at
    if created_utc:
        comp_utc = job.completed_at.replace(tzinfo=UTC) if (job.completed_at and job.completed_at.tzinfo is None) else job.completed_at
        deliv_utc = job.delivered_at.replace(tzinfo=UTC) if (job.delivered_at and job.delivered_at.tzinfo is None) else job.delivered_at
        end_time = comp_utc or deliv_utc or datetime.now(UTC)
        total_sec = (end_time - created_utc).total_seconds()
        app_req_utc = job.approval_requested_at.replace(tzinfo=UTC) if (job.approval_requested_at and job.approval_requested_at.tzinfo is None) else job.approval_requested_at
        app_done_utc = job.approved_at.replace(tzinfo=UTC) if (job.approved_at and job.approved_at.tzinfo is None) else job.approved_at
        if app_done_utc and app_req_utc:
            hold_sec = (app_done_utc - app_req_utc).total_seconds()
            active_sec = max(1, int(total_sec - hold_sec))
        else:
            active_sec = max(1, int(total_sec))
        proc_time_str = format_duration_sec(active_sec)

    created_str = created_utc.strftime("%Y-%m-%d %H:%M:%S UTC") if created_utc else "N/A"

    # AI identity & tokens
    ai_prov, ai_model = get_job_ai_identity(job)
    if job.request_mode == "literal":
        ai_line = "• <b>AI:</b> <code>None (Literal mode)</code>"
    elif ai_prov and ai_model:
        ai_line = f"• <b>AI Model:</b> <code>{html.escape(ai_prov)} ({html.escape(ai_model)})</code>"
    else:
        ai_line = "• <b>AI:</b> <code>None</code>"

    # TTS chunks count
    chunks_str = ""
    if db:
        tts_count = db.query(PodcastTTSChunk).filter(PodcastTTSChunk.job_id == job.id).count()
        if tts_count > 0:
            chunks_str = f"\n• <b>TTS Chunks:</b> {tts_count}"
        ai_calls = db.query(AIInteraction).filter(AIInteraction.job_id == job.id).all()
        if ai_calls:
            tot_tok = sum(c.total_tokens for c in ai_calls if c.total_tokens is not None)
            tok_str = f" ({tot_tok:,} tokens)" if tot_tok else ""
            ai_line += f"\n• <b>AI Interactions:</b> {len(ai_calls)} call(s){tok_str}"

    # Retries / Errors
    retry_parts = []
    if job.attempt_count and job.attempt_count > 1:
        retry_parts.append(f"intake: {job.attempt_count}")
    if job.synthesis_attempt_count and job.synthesis_attempt_count > 1:
        retry_parts.append(f"synthesis: {job.synthesis_attempt_count}")
    if job.delivery_attempt_count and job.delivery_attempt_count > 1:
        retry_parts.append(f"delivery: {job.delivery_attempt_count}")
    if job.verify_repair_count:
        retry_parts.append(f"repair: {job.verify_repair_count}")
    retries_line = f"\n• <b>Retries:</b> {', '.join(retry_parts)}" if retry_parts else ""

    error_section = ""
    if job.error_code or job.failed_stage:
        err_stage = html.escape(job.failed_stage or "UNKNOWN")
        err_code = html.escape(job.error_code or "ERROR")
        err_det = html.escape(job.error_detail[:200] + "..." if job.error_detail and len(job.error_detail) > 200 else (job.error_detail or ""))
        error_section = (
            f"\n\n⚠️ <b>Failure Details:</b>\n"
            f"• <b>Stage:</b> <code>{err_stage}</code>\n"
            f"• <b>Error:</b> <code>{err_code}</code>\n"
            f"• <i>{err_det}</i>"
        )

    audio_line = ""
    if job.audio_duration_seconds:
        dur_str = format_duration_sec(job.audio_duration_seconds)
        size_mb = (job.audio_bytes / (1024 * 1024)) if job.audio_bytes else 0
        audio_line = f"\n• <b>Audio Output:</b> {dur_str} ({size_mb:.1f} MB)"

    card = (
        f"🛠️ <b>Diagnostics: {title}</b>\n\n"
        f"• <b>Job ID:</b> <code>{short_id}</code> (<code>{job.id}</code>)\n"
        f"• <b>Status:</b> <code>{status}</code>\n"
        f"• <b>Mode:</b> {mode_esc}\n"
        f"• <b>Source Type:</b> <code>{src_type}</code>\n"
        f"{ai_line}\n"
        f"• <b>Voice & Speed:</b> <code>{voice}</code> @ {speed:.1f}x"
        f"{chunks_str}"
        f"{audio_line}\n"
        f"• <b>Created:</b> {created_str}\n"
        f"• <b>Processing Time:</b> {proc_time_str}"
        f"{retries_line}"
        f"{error_section}\n\n"
        f"📦 <i>Downloading diagnostic support package (ZIP)...</i>"
    )

    if len(card) > 4000:
        card = card[:3900] + "\n\n<i>[Truncated to fit Telegram limits]</i>"

    return card


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
