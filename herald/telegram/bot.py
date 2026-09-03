import html
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy.orm import Session

from herald.ai.factory import get_ai_provider
from herald.audio.ffmpeg_builder import check_free_disk_mb
from herald.config import settings
from herald.core.models import HeraldRequest, HeraldResponse
from herald.core.pipeline import process_herald_request
from herald.db.connection import SessionLocal
from herald.db.models import (
    JobState,
    JobStateTransition,
    PodcastJob,
    TelegramPollState,
    TelegramUpdateFailure,
)
from herald.extraction.email_parser import (
    URL_REGEX,
)
from herald.services.eta_calculator import calculate_job_eta
from herald.services.voice_manager import (
    VOICE_METADATA,
    ensure_voice_sample,
    get_voice_sample_path,
    is_valid_sample_audio,
)
from herald.telegram.auth import (
    get_effective_user_preferences,
    get_paired_owner,
    has_owner,
    is_user_authorized,
    set_user_confirm_before_tts,
    set_user_default_voice,
    verify_and_claim_pairing_code,
)
from herald.telegram.client import TelegramClient
from herald.telegram.delivery import deliver_job_download, deliver_pending_telegram_jobs
from herald.telegram.formatters import (
    format_approval,
    format_help,
    format_queued,
    format_quickstart,
    format_settings,
    format_voices_browser,
    get_job_display_title,
)
from herald.telegram.resolver import resolve_user_job
from herald.tts.kokoro_client import KokoroClient

logger = logging.getLogger("herald.telegram.bot")

_START_TIME = datetime.now(UTC)

# Dedicated bounded background threadpool for voice sample pre-rendering (cache miss)
_VOICE_SAMPLE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voice-sample")
_IN_FLIGHT_VOICE_SAMPLES: set[str] = set()
_VOICE_SAMPLE_LOCK = Lock()


def parse_telegram_message_directives(text: str) -> dict[str, Any]:
    """
    Parse mode, directives, URLs, and body text from a Telegram message.
    Strictly consumes recognized directive header lines from the start of the message.
    Once the first substantive non-directive line is reached, directive parsing ends permanently.
    """
    if not text:
        return {
            "mode": settings.get_default_mode(),
            "research_depth": None,
            "url": None,
            "text": "",
            "voice": None,
            "speed": None,
            "title": None,
            "chunk_chars": 500,
            "verify": False,
        }

    lines = text.splitlines()
    matched_mode = None
    matched_depth = None
    voice = None
    speed = None
    title = None
    chunk_chars = 500
    verify = False
    in_directive_header = True
    url_in_header = None
    body_lines = []

    allowed_voices = settings.get_allowed_voices_list()

    for line in lines:
        stripped = line.strip()

        if in_directive_header:
            if not stripped:
                # Blank lines inside top directive header region are allowed
                continue

            lower_line = stripped.lower()

            # Standalone URL in header zone
            if stripped.startswith(("http://", "https://")) and " " not in stripped:
                url_in_header = stripped
                continue

            # Mode keywords
            if lower_line in ("literal", "podcast: literal", "mode: literal"):
                matched_mode = "literal"
                continue
            elif lower_line in ("brief", "podcast: brief", "mode: brief"):
                matched_mode = "brief"
                continue
            elif lower_line in ("standard", "podcast: standard", "mode: standard"):
                matched_mode = "standard"
                continue
            elif lower_line.startswith("research") or lower_line.startswith("podcast: research") or lower_line.startswith("detailed"):
                matched_mode = "research"
                if "low" in lower_line:
                    matched_depth = "low"
                elif "high" in lower_line:
                    matched_depth = "high"
                else:
                    matched_depth = "medium"
                continue
            elif lower_line == "verify":
                verify = True
                continue
            elif lower_line.startswith("chunk-"):
                val_str = lower_line[len("chunk-"):]
                if val_str.isdigit():
                    c_val = int(val_str)
                    if not (settings.TTS_CHUNK_MIN_CHARS <= c_val <= settings.TTS_CHUNK_MAX_CHARS):
                        raise ValueError(
                            f"Invalid chunk size: {c_val}. Must be between "
                            f"{settings.TTS_CHUNK_MIN_CHARS} and {settings.TTS_CHUNK_MAX_CHARS}."
                        )
                    chunk_chars = c_val
                else:
                    raise ValueError(f"Invalid chunk directive: {stripped}")
                continue

            # Header directives
            if lower_line.startswith("voice:"):
                v_val = stripped.split(":", 1)[1].strip().lower()
                if v_val in allowed_voices:
                    voice = v_val
                else:
                    raise ValueError(f"Invalid voice '{v_val}'. Allowed: {allowed_voices}")
                continue
            elif lower_line.startswith("speed:"):
                s_val = stripped.split(":", 1)[1].strip()
                try:
                    s_float = float(s_val)
                    if not (settings.MIN_SPEED <= s_float <= settings.MAX_SPEED):
                        raise ValueError(
                            f"Speed {s_float} out of range ({settings.MIN_SPEED} to {settings.MAX_SPEED})."
                        )
                    speed = s_float
                except ValueError as ve:
                    raise ValueError(f"Invalid speed directive '{s_val}': {ve}")
                continue
            elif lower_line.startswith("title:"):
                t_val = stripped.split(":", 1)[1].strip()
                if t_val:
                    title = t_val
                continue
            elif lower_line.startswith("research:"):
                r_val = stripped.split(":", 1)[1].strip().lower()
                if r_val in ("low", "medium", "high"):
                    matched_depth = r_val
                continue

            # First substantive line that is NOT a directive ends directive header zone
            in_directive_header = False
            body_lines.append(line)
        else:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()

    # Determine if body is a standalone article URL vs substantial text containing links
    detected_url = None
    if url_in_header and not body:
        detected_url = url_in_header
    elif not url_in_header and body:
        all_urls = URL_REGEX.findall(body)
        if all_urls:
            body_without_url = URL_REGEX.sub("", body).strip()
            if len(all_urls) == 1 and len(body_without_url) < 10:
                detected_url = all_urls[0]
                body = ""
    elif url_in_header and body:
        # If both URL in header and body lines exist, body is preserved and URL is part of source
        body = f"{url_in_header}\n\n{body}".strip()

    mode = matched_mode or settings.get_default_mode()

    return {
        "mode": mode,
        "explicit_mode": matched_mode,
        "research_depth": matched_depth,
        "url": detected_url,
        "text": body,
        "voice": voice,
        "speed": speed,
        "title": title,
        "chunk_chars": chunk_chars,
        "verify": verify,
    }


def handle_telegram_command(
    db: Session,
    client: TelegramClient,
    message: dict[str, Any],
    command: str,
    args: str,
) -> None:
    """Handle Telegram bot slash commands."""
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type") or ("private" if (isinstance(chat_id, int) and chat_id > 0) else ("group" if isinstance(chat_id, int) and chat_id < 0 else "private"))
    user = message.get("from", {})
    user_id = user.get("id")
    msg_id = message.get("message_id")
    cmd_clean = (command or "").lstrip("/").lower()

    if not chat_id or not user_id:
        return

    # Enforce private chat only
    if chat_type != "private":
        client.send_message(
            chat_id=chat_id,
            text="⚠️ <b>Herald operates only in private chats.</b>",
            reply_to_message_id=msg_id,
            parse_mode="HTML",
        )
        return

    # Handle /pair command (available before authorization)
    if cmd_clean == "pair":
        if not args:
            client.send_message(
                chat_id=chat_id,
                text="⚠️ Usage: <code>/pair &lt;code&gt;</code>\n\nEnter the 6-digit pairing code displayed in your Herald console.",
                reply_to_message_id=msg_id,
                parse_mode="HTML",
            )
            return

        success, reply_msg = verify_and_claim_pairing_code(
            db=db,
            code=args.strip(),
            user_id=user_id,
            chat_id=chat_id,
            username=user.get("username"),
            first_name=user.get("first_name"),
        )
        escaped_reply = html.escape(reply_msg)
        if success:
            owner = get_paired_owner(db)
            owner_name = (owner.first_name or owner.username or str(owner.telegram_user_id)) if owner else "Owner"
            default_mode = settings.get_default_mode()
            ai_prov = settings.AI_PROVIDER or "None (Literal only)"
            quickstart_msg = format_quickstart(owner_name=owner_name, default_mode=default_mode, ai_provider=ai_prov)
            client.send_message(
                chat_id=chat_id,
                text=f"✅ <b>{escaped_reply}</b>\n\n{quickstart_msg}",
                reply_to_message_id=msg_id,
                parse_mode="HTML",
            )
        else:
            client.send_message(
                chat_id=chat_id,
                text=f"❌ <b>{escaped_reply}</b>",
                reply_to_message_id=msg_id,
                parse_mode="HTML",
            )
        return

    # Authorization check for all other commands
    if not is_user_authorized(db, user_id=user_id, chat_id=chat_id):
        if not has_owner(db):
            client.send_message(
                chat_id=chat_id,
                text="🔒 <b>Herald Instance Unpaired</b>\n\nThis Herald instance has not been paired yet. Check the server console for the pairing code and run <code>/pair &lt;code&gt;</code> to claim ownership.",
                reply_to_message_id=msg_id,
                parse_mode="HTML",
            )
        else:
            client.send_message(
                chat_id=chat_id,
                text="⛔ <b>Access Denied</b>\n\nYou are not authorized to use this Herald instance.",
                reply_to_message_id=msg_id,
                parse_mode="HTML",
            )
        return

    if cmd_clean == "start":
        owner = get_paired_owner(db)
        owner_name = (owner.first_name or owner.username or str(owner.telegram_user_id)) if owner else "Owner"
        default_mode = settings.get_default_mode()
        ai_prov = settings.AI_PROVIDER or "None (Literal only)"
        quickstart_msg = format_quickstart(owner_name=owner_name, default_mode=default_mode, ai_provider=ai_prov)
        client.send_message(
            chat_id=chat_id,
            text=quickstart_msg,
            reply_to_message_id=msg_id,
            parse_mode="HTML",
        )

    elif cmd_clean == "help":
        client.send_message(chat_id=chat_id, text=format_help(), reply_to_message_id=msg_id, parse_mode="HTML")

    elif cmd_clean == "status":
        uptime_seconds = int((datetime.now(UTC) - _START_TIME).total_seconds())
        hrs, rem = divmod(uptime_seconds, 3600)
        mins, secs = divmod(rem, 60)
        uptime_str = f"{hrs}h {mins}m {secs}s"

        work_dir = Path(settings.HERALD_WORK_DIR)
        free_mb = check_free_disk_mb(work_dir)

        # Kokoro health
        kokoro_client = KokoroClient()
        kokoro_res = kokoro_client.health_check()
        kokoro_status = "🟢 Healthy" if kokoro_res.get("healthy") else "🔴 Unreachable"

        # AI Provider cached health check
        ai_provider = get_ai_provider()
        if ai_provider and ai_provider.is_configured():
            ai_res = ai_provider.check_connection(timeout_seconds=3.0, force_refresh=False)
            if ai_res.get("connected"):
                ai_status_str = f"🟢 Connected ({html.escape(ai_res.get('model', ''))})"
            else:
                ai_status_str = f"🔴 {html.escape(ai_res.get('error') or 'Error')}"
        else:
            ai_status_str = "⚪ Not configured (Literal mode default)"

        # Queue counts across all nonterminal active states
        nonterminal_states = [
            s.value for s in JobState
            if s not in (JobState.COMPLETE, JobState.FAILED_FINAL, JobState.CANCELLED)
        ]
        active_count = (
            db.query(PodcastJob)
            .filter(PodcastJob.status.in_(nonterminal_states))
            .count()
        )
        completed_count = db.query(PodcastJob).filter(PodcastJob.status == JobState.COMPLETE.value).count()

        status_msg = (
            f"📊 <b>Herald System Status</b>\n\n"
            f"• <b>Uptime:</b> {html.escape(uptime_str)}\n"
            f"• <b>TTS Engine (Kokoro):</b> {kokoro_status}\n"
            f"• <b>AI Provider:</b> {ai_status_str}\n"
            f"• <b>Disk Space:</b> {free_mb:.1f} MB free\n"
            f"• <b>Active Jobs:</b> {active_count}\n"
            f"• <b>Completed Jobs:</b> {completed_count}\n"
        )
        client.send_message(chat_id=chat_id, text=status_msg, reply_to_message_id=msg_id, parse_mode="HTML")

    elif cmd_clean in ("ai_check", "ai-check", "aicheck"):
        ai_provider = get_ai_provider()
        if not ai_provider or not ai_provider.is_configured():
            client.send_message(
                chat_id=chat_id,
                text="ℹ️ <b>AI Provider is not configured.</b>\nHerald is running in deterministic <b>Literal</b> mode (no AI API keys required).",
                reply_to_message_id=msg_id,
                parse_mode="HTML",
            )
            return

        client.send_message(
            chat_id=chat_id,
            text="🔄 <i>Testing AI provider connection...</i>",
            reply_to_message_id=msg_id,
            parse_mode="HTML",
        )
        res = ai_provider.check_connection(timeout_seconds=5.0, force_refresh=True)
        prov_name = html.escape(res.get("provider", "AI"))
        model_name = html.escape(res.get("model", "default"))
        if res.get("connected"):
            client.send_message(
                chat_id=chat_id,
                text=f"✅ <b>{prov_name} Connected Successfully!</b>\n\nModel: <code>{model_name}</code>\nStatus: Ready for brief, standard, and research requests.",
                parse_mode="HTML",
            )
        else:
            err = html.escape(res.get("error") or "Unknown error")
            client.send_message(
                chat_id=chat_id,
                text=f"❌ <b>{prov_name} Connection Failed</b>\n\nError: <code>{err}</code>\n\nNote: Literal mode remains 100% operational.",
                parse_mode="HTML",
            )

    elif cmd_clean == "queue":
        owner = get_paired_owner(db)
        is_owner = (owner is not None and int(user_id) == owner.telegram_user_id)

        q = db.query(PodcastJob).filter(
            PodcastJob.status.notin_([JobState.COMPLETE.value, JobState.FAILED_FINAL.value, JobState.CANCELLED.value])
        )
        if not is_owner:
            q = q.filter(
                PodcastJob.transport == "telegram",
                PodcastJob.telegram_user_id == int(user_id),
                PodcastJob.telegram_chat_id == int(chat_id),
            )

        jobs = q.order_by(PodcastJob.created_at.asc()).limit(10).all()
        if not jobs:
            client.send_message(chat_id=chat_id, text="📭 <b>Queue is currently empty.</b>", reply_to_message_id=msg_id, parse_mode="HTML")
            return

        lines = ["📋 <b>Active Podcast Queue:</b>\n"]
        for j in jobs:
            t = html.escape(get_job_display_title(j))
            mode = html.escape(j.request_mode or "standard")
            st = html.escape(j.status)
            lines.append(f"• <b>{t}</b> [{mode}] — <code>{st}</code>")

        client.send_message(chat_id=chat_id, text="\n".join(lines), reply_to_message_id=msg_id, parse_mode="HTML")

    elif cmd_clean == "settings":
        prefs = get_effective_user_preferences(db, user_id)
        settings_text, reply_markup = format_settings(prefs, settings)
        client.send_message(
            chat_id=chat_id,
            text=settings_text,
            reply_to_message_id=msg_id,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    elif cmd_clean == "voices":
        user_prefs = get_effective_user_preferences(db, user_id)
        voices_text, reply_markup = format_voices_browser(
            current_default=user_prefs.get("default_voice", "af_heart")
        )
        client.send_message(
            chat_id=chat_id,
            text=voices_text,
            reply_markup=reply_markup,
            reply_to_message_id=msg_id,
            parse_mode="HTML",
        )

    elif cmd_clean == "download":
        job = resolve_user_job(db, telegram_user_id=user_id, telegram_chat_id=chat_id, identifier=args, completed_only=True)
        if not job:
            client.send_message(
                chat_id=chat_id,
                text="ℹ️ <b>No completed podcast found to download.</b>\n\nProvide a valid episode ID (e.g. <code>/download &lt;id&gt;</code>) or generate a podcast first.",
                reply_to_message_id=msg_id,
                parse_mode="HTML",
            )
            return

        deliver_job_download(db, client, job, chat_id=chat_id, reply_to_message_id=msg_id)

    elif cmd_clean == "readme":
        readme_path = Path("README.md")
        if readme_path.exists():
            client.send_document(
                chat_id=chat_id,
                document_path=str(readme_path),
                caption="📄 Herald Project README",
                reply_to_message_id=msg_id,
            )
        else:
            client.send_message(chat_id=chat_id, text="README.md not found on server.", reply_to_message_id=msg_id)

    else:
        client.send_message(
            chat_id=chat_id,
            text=f"❓ Unknown command: <code>/{html.escape(command)}</code>\nUse /help to view available commands.",
            reply_to_message_id=msg_id,
            parse_mode="HTML",
        )


def handle_telegram_content_message(
    db: Session,
    client: TelegramClient,
    message: dict[str, Any],
) -> None:
    """Handle plain text, forwarded message, or URL input from an authorized user."""
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type") or ("private" if (isinstance(chat_id, int) and chat_id > 0) else ("group" if isinstance(chat_id, int) and chat_id < 0 else "private"))
    user = message.get("from", {})
    user_id = user.get("id")
    msg_id = message.get("message_id")

    if not chat_id or not user_id:
        return

    # Private chat check
    if chat_type != "private":
        client.send_message(
            chat_id=chat_id,
            text="⚠️ <b>Herald operates only in private chats.</b>",
            reply_to_message_id=msg_id,
            parse_mode="HTML",
        )
        return

    if not is_user_authorized(db, user_id=user_id, chat_id=chat_id):
        if not has_owner(db):
            client.send_message(
                chat_id=chat_id,
                text="🔒 <b>Herald Instance Unpaired</b>\n\nPlease pair with this Herald instance first by running <code>/pair &lt;code&gt;</code>.",
                reply_to_message_id=msg_id,
                parse_mode="HTML",
            )
        else:
            client.send_message(
                chat_id=chat_id,
                text="⛔ <b>Access Denied</b>\n\nYou are not authorized to use this Herald instance.",
                reply_to_message_id=msg_id,
                parse_mode="HTML",
            )
        return

    text = message.get("text") or message.get("caption") or ""
    if not text.strip():
        client.send_message(
            chat_id=chat_id,
            text="⚠️ Please send an article URL, pasted text, or forwarded message.",
            reply_to_message_id=msg_id,
            parse_mode="HTML",
        )
        return

    try:
        parsed = parse_telegram_message_directives(text)
    except ValueError as ve:
        client.send_message(
            chat_id=chat_id,
            text=f"⚠️ <b>Directive Error:</b> {html.escape(str(ve))}",
            reply_to_message_id=msg_id,
            parse_mode="HTML",
        )
        return

    user_prefs = get_effective_user_preferences(db, user_id)
    eff_mode = parsed.get("explicit_mode") or user_prefs.get("default_mode") or settings.get_default_mode()
    eff_voice = parsed.get("voice") or user_prefs.get("default_voice") or settings.KOKORO_VOICE
    eff_speed = parsed.get("speed") if parsed.get("speed") is not None else user_prefs.get("default_speed", settings.KOKORO_SPEED)
    confirm_tts = bool(user_prefs.get("confirm_before_tts", False))

    req = HeraldRequest(
        transport="telegram",
        transport_message_id=msg_id,
        requester_identity=f"telegram:{user_id}",
        delivery_target=str(chat_id),
        request_mode=eff_mode,
        research_depth=parsed["research_depth"],
        source_url=parsed["url"],
        source_text=parsed["text"] if not parsed["url"] else None,
        custom_voice=eff_voice,
        custom_speed=eff_speed,
        custom_title=parsed["title"],
        tts_chunk_chars=parsed["chunk_chars"],
        verify_final_script=parsed["verify"],
        hold_for_approval=confirm_tts,
    )

    try:
        response: HeraldResponse = process_herald_request(db=db, req=req)
    except Exception as e:
        logger.exception("Error processing Telegram request")
        client.send_message(
            chat_id=chat_id,
            text=f"❌ <b>Error processing request:</b> {html.escape(str(e))}",
            reply_to_message_id=msg_id,
            parse_mode="HTML",
        )
        return

    title_escaped = html.escape(response.episode_title or "Podcast Episode")
    mode_escaped = html.escape(response.request_mode)

    if response.is_duplicate:
        existing_job = db.query(PodcastJob).filter_by(id=response.job_id).first()
        if existing_job:
            # Recovery path: job is awaiting approval but card was never delivered
            if existing_job.status == JobState.AWAITING_APPROVAL.value and not existing_job.telegram_approval_message_id:
                eta_info = calculate_job_eta(db, existing_job)
                app_text, reply_markup = format_approval(existing_job, existing_job.script_json, eta_info)
                try:
                    sent_msg = client.send_message(
                        chat_id=chat_id,
                        text=app_text,
                        reply_markup=reply_markup,
                        reply_to_message_id=msg_id,
                        parse_mode="HTML",
                    )
                    if sent_msg and isinstance(sent_msg, dict) and sent_msg.get("message_id"):
                        existing_job.telegram_approval_message_id = sent_msg["message_id"]
                        existing_job.approval_requested_at = datetime.now(UTC)
                        db.commit()
                except Exception as e:
                    logger.error(f"Failed to deliver recovery approval card for job '{existing_job.id}': {e}")
                return

            if existing_job.status == JobState.COMPLETE.value and existing_job.local_audio_path and os.path.exists(existing_job.local_audio_path):
                client.send_message(
                    chat_id=chat_id,
                    text=f"🎧 <b>Already Processed:</b> Re-delivering '{title_escaped}'...",
                    reply_to_message_id=msg_id,
                    parse_mode="HTML",
                )
                client.send_audio(
                    chat_id=chat_id,
                    audio_path=existing_job.local_audio_path,
                    title=response.episode_title or "Herald Episode",
                    performer="Herald",
                    caption=f"🎙️ <b>{title_escaped}</b>\nFormat: {mode_escaped}\n(Re-delivered)",
                    reply_to_message_id=msg_id,
                )
                return

        client.send_message(
            chat_id=chat_id,
            text=f"ℹ️ <b>Already Received:</b> '{title_escaped}' [{mode_escaped}] is currently <code>{html.escape(response.status)}</code>.",
            reply_to_message_id=msg_id,
            parse_mode="HTML",
        )
        return

    job = db.query(PodcastJob).filter_by(id=response.job_id).first()
    eta_info = calculate_job_eta(db, job) if job else {}

    if response.status == JobState.AWAITING_APPROVAL.value and job:
        app_text, reply_markup = format_approval(job, job.script_json, eta_info)
        try:
            sent_msg = client.send_message(
                chat_id=chat_id,
                text=app_text,
                reply_markup=reply_markup,
                reply_to_message_id=msg_id,
                parse_mode="HTML",
            )
            if sent_msg and isinstance(sent_msg, dict) and sent_msg.get("message_id"):
                job.telegram_approval_message_id = sent_msg["message_id"]
                job.approval_requested_at = datetime.now(UTC)
                db.commit()
        except Exception as e:
            logger.error(f"Failed to deliver Telegram approval card for job '{job.id}': {e}")
        return

    # Rich queued card (confirmation is OFF or directly queued)
    if job:
        queued_text = format_queued(job, job.script_json, eta_info)
    else:
        queued_text = (
            f"🎙️ <b>Podcast Queued!</b>\n\n"
            f"• <b>Title:</b> {title_escaped}\n"
            f"• <b>Format:</b> {mode_escaped}\n"
            f"• <b>Status:</b> <code>{html.escape(response.status)}</code>\n\n"
            f"Synthesizing audio now. You will receive the completed MP3 file shortly."
        )

    client.send_message(
        chat_id=chat_id,
        text=queued_text,
        reply_to_message_id=msg_id,
        parse_mode="HTML",
    )


def handle_telegram_callback_query(
    db: Session,
    client: TelegramClient,
    cb_query: dict[str, Any],
) -> None:
    """
    Handle inline keyboard callback queries with strict private chat, authorization,
    UTF-8 length validation, and SET-semantics idempotency.
    """
    cb_id = str(cb_query.get("id") or "")
    if not cb_id:
        return

    raw_data = str(cb_query.get("data") or "")
    # Validate UTF-8 encoded byte length <= 64 bytes
    if len(raw_data.encode("utf-8")) > 64:
        logger.warning(f"Rejected oversized callback data ({len(raw_data.encode('utf-8'))} bytes)")
        client.answer_callback_query(cb_id, text="Error: Callback data too large.", show_alert=True)
        return

    from_user = cb_query.get("from") or {}
    user_id = from_user.get("id")
    message = cb_query.get("message") or {}
    msg_id = message.get("message_id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")

    if not chat_id or not user_id or not msg_id:
        client.answer_callback_query(cb_id, text="Error: Incomplete callback context.")
        return

    # Strict private chat guard
    if chat_type != "private":
        client.answer_callback_query(cb_id, text="Herald operates only in private chats.", show_alert=True)
        return

    # Owner authorization guard
    if not is_user_authorized(db, user_id=user_id, chat_id=chat_id):
        client.answer_callback_query(cb_id, text="Unauthorized: Access denied.", show_alert=True)
        return

    # Route versioned callbacks with SET-semantics
    if raw_data == "h2:settings:confirm:on":
        set_user_confirm_before_tts(db, user_id=user_id, chat_id=chat_id, enabled=True)
        # Acknowledge callback immediately to dismiss Telegram spinner
        client.answer_callback_query(cb_id, text="Confirm Before TTS enabled.")
        # Update settings message text and markup
        prefs = get_effective_user_preferences(db, user_id)
        settings_text, reply_markup = format_settings(prefs, settings)
        try:
            client.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=settings_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except Exception as e:
            if "message is not modified" in str(e).lower():
                logger.debug(f"editMessageText idempotent notice: {e}")
            else:
                logger.warning(f"Failed to update settings message markup: {e}")
        return

    elif raw_data == "h2:settings:confirm:off":
        set_user_confirm_before_tts(db, user_id=user_id, chat_id=chat_id, enabled=False)
        # Acknowledge callback immediately to dismiss Telegram spinner
        client.answer_callback_query(cb_id, text="Confirm Before TTS disabled.")
        # Update settings message text and markup
        prefs = get_effective_user_preferences(db, user_id)
        settings_text, reply_markup = format_settings(prefs, settings)
        try:
            client.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=settings_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except Exception as e:
            if "message is not modified" in str(e).lower():
                logger.debug(f"editMessageText idempotent notice: {e}")
            else:
                logger.warning(f"Failed to update settings message markup: {e}")
        return

    elif raw_data.startswith("h2:approve:"):
        job_id = raw_data[len("h2:approve:"):]
        job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
        if not job or job.telegram_user_id != user_id or job.telegram_chat_id != chat_id or job.transport != "telegram":
            client.answer_callback_query(cb_id, text="Unauthorized: Access denied.", show_alert=True)
            return

        now = datetime.now(UTC)
        updated_count = (
            db.query(PodcastJob)
            .filter(
                PodcastJob.id == job_id,
                PodcastJob.status == JobState.AWAITING_APPROVAL.value,
                PodcastJob.telegram_user_id == user_id,
                PodcastJob.telegram_chat_id == chat_id,
            )
            .update(
                {
                    "status": JobState.QUEUED_TTS.value,
                    "approved_at": now,
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        )

        if updated_count == 1:
            transition_rec = JobStateTransition(
                job_id=job_id,
                from_state=JobState.AWAITING_APPROVAL.value,
                to_state=JobState.QUEUED_TTS.value,
                component="telegram-approval",
                message="Approved by user",
                created_at=now,
            )
            db.add(transition_rec)
            db.commit()
            db.refresh(job)

            client.answer_callback_query(cb_id, text="Approved! Queued for synthesis.")
            eta_info = calculate_job_eta(db, job)
            queued_text = format_queued(job, job.script_json, eta_info)
            try:
                client.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=queued_text,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except Exception as e:
                logger.debug(f"Failed to edit approval message to queued card: {e}")
            return
        else:
            db.rollback()
            db.refresh(job)
            if job.status in (
                JobState.QUEUED_TTS.value,
                JobState.SYNTHESIZING.value,
                JobState.ENCODING.value,
                JobState.COMPLETE.value,
            ):
                client.answer_callback_query(cb_id, text="Job is already approved and synthesizing.")
                return
            elif job.status == JobState.CANCELLED.value:
                client.answer_callback_query(cb_id, text="Job was already cancelled.", show_alert=True)
                return
            else:
                client.answer_callback_query(cb_id, text=f"Cannot approve job in state {job.status}.", show_alert=True)
                return

    elif raw_data.startswith("h2:deny:"):
        job_id = raw_data[len("h2:deny:"):]
        job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
        if not job or job.telegram_user_id != user_id or job.telegram_chat_id != chat_id or job.transport != "telegram":
            client.answer_callback_query(cb_id, text="Unauthorized: Access denied.", show_alert=True)
            return

        now = datetime.now(UTC)
        updated_count = (
            db.query(PodcastJob)
            .filter(
                PodcastJob.id == job_id,
                PodcastJob.status == JobState.AWAITING_APPROVAL.value,
                PodcastJob.telegram_user_id == user_id,
                PodcastJob.telegram_chat_id == chat_id,
            )
            .update(
                {
                    "status": JobState.CANCELLED.value,
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        )

        if updated_count == 1:
            transition_rec = JobStateTransition(
                job_id=job_id,
                from_state=JobState.AWAITING_APPROVAL.value,
                to_state=JobState.CANCELLED.value,
                component="telegram-approval",
                message="Cancelled by user",
                created_at=now,
            )
            db.add(transition_rec)
            db.commit()

            client.answer_callback_query(cb_id, text="Generation cancelled.")
            try:
                client.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text="❌ <b>Podcast Generation Cancelled.</b>",
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except Exception as e:
                logger.debug(f"Failed to edit message to cancelled: {e}")
            return
        else:
            db.rollback()
            db.refresh(job)
            if job.status == JobState.CANCELLED.value:
                client.answer_callback_query(cb_id, text="Job already cancelled.")
                return
            elif job.status in (
                JobState.QUEUED_TTS.value,
                JobState.SYNTHESIZING.value,
                JobState.ENCODING.value,
                JobState.COMPLETE.value,
            ):
                client.answer_callback_query(cb_id, text="Job is already synthesizing and cannot be cancelled.", show_alert=True)
                return
            else:
                client.answer_callback_query(cb_id, text=f"Cannot cancel job in state {job.status}.", show_alert=True)
                return

    elif raw_data.startswith("h2:download:"):
        job_id = raw_data[len("h2:download:"):]
        job = resolve_user_job(db, telegram_user_id=user_id, telegram_chat_id=chat_id, identifier=job_id, completed_only=True)
        if not job:
            client.answer_callback_query(cb_id, text="Podcast not found or access denied.", show_alert=True)
            return

        client.answer_callback_query(cb_id, text="Sending MP3 file...")
        deliver_job_download(db, client, job, chat_id=chat_id)
        return

    elif raw_data.startswith("h2:voice:set:"):
        v_name = raw_data[len("h2:voice:set:"):]
        allowed = settings.get_allowed_voices_list()
        if v_name not in allowed:
            client.answer_callback_query(cb_id, text=f"Invalid voice '{v_name}'.", show_alert=True)
            return

        set_user_default_voice(db, user_id=user_id, voice=v_name, chat_id=chat_id)
        meta = VOICE_METADATA.get(v_name, {})
        disp_name = meta.get("display_name", v_name)
        client.answer_callback_query(cb_id, text=f"Default voice set to {disp_name} ({v_name}).")
        voices_text, reply_markup = format_voices_browser(current_default=v_name)
        try:
            client.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=voices_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.debug(f"Failed to edit voices browser: {e}")
        return

    elif raw_data.startswith("h2:voice:sample:"):
        v_name = raw_data[len("h2:voice:sample:"):]
        allowed = settings.get_allowed_voices_list()
        if v_name not in allowed:
            client.answer_callback_query(cb_id, text=f"Invalid voice '{v_name}'.", show_alert=True)
            return

        meta = VOICE_METADATA.get(v_name, {})
        disp_name = meta.get("display_name", v_name)
        sample_path = get_voice_sample_path(v_name)

        sample_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": f"⭐ Set as Default ({disp_name})",
                        "callback_data": f"h2:voice:set:{v_name}",
                    }
                ]
            ]
        }

        # Cache hit: fast immediate delivery
        if is_valid_sample_audio(sample_path):
            client.answer_callback_query(cb_id, text=f"Playing sample for {disp_name}...")
            caption = f"🎙️ <b>Voice Sample:</b> <code>{html.escape(v_name)}</code> ({html.escape(disp_name)})\nSpeed: 1.0x"
            client.send_audio(
                chat_id=chat_id,
                audio_path=sample_path,
                title=f"Sample: {disp_name}",
                performer="Herald",
                caption=caption,
                parse_mode="HTML",
                reply_markup=sample_markup,
            )
            return

        # Cache miss: non-blocking background generation with in-flight deduplication
        with _VOICE_SAMPLE_LOCK:
            if v_name in _IN_FLIGHT_VOICE_SAMPLES:
                client.answer_callback_query(
                    cb_id,
                    text=f"Sample for {disp_name} is already being prepared...",
                    show_alert=False,
                )
                return
            _IN_FLIGHT_VOICE_SAMPLES.add(v_name)

        client.answer_callback_query(cb_id, text=f"Preparing voice sample for {disp_name}... Herald will send it shortly.")

        def _bg_generate_sample(target_voice: str, target_chat_id: int):
            try:
                with SessionLocal() as db_session:
                    gen_path = ensure_voice_sample(voice=target_voice, db=db_session)
                v_meta = VOICE_METADATA.get(target_voice, {})
                d_name = v_meta.get("display_name", target_voice)
                cap = f"🎙️ <b>Voice Sample:</b> <code>{html.escape(target_voice)}</code> ({html.escape(d_name)})\nSpeed: 1.0x"
                m_up = {
                    "inline_keyboard": [
                        [
                            {
                                "text": f"⭐ Set as Default ({d_name})",
                                "callback_data": f"h2:voice:set:{target_voice}",
                            }
                        ]
                    ]
                }
                client.send_audio(
                    chat_id=target_chat_id,
                    audio_path=gen_path,
                    title=f"Sample: {d_name}",
                    performer="Herald",
                    caption=cap,
                    parse_mode="HTML",
                    reply_markup=m_up,
                )
            except Exception as e:
                logger.error(f"Background voice sample generation failed for '{target_voice}': {e}", exc_info=True)
                try:
                    client.send_message(
                        chat_id=target_chat_id,
                        text="⚠️ <b>Voice sample generation failed. Please try again.</b>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            finally:
                with _VOICE_SAMPLE_LOCK:
                    _IN_FLIGHT_VOICE_SAMPLES.discard(target_voice)

        _VOICE_SAMPLE_EXECUTOR.submit(_bg_generate_sample, v_name, int(chat_id))
        return

    else:
        # Unknown / future callback actions safely acknowledged
        logger.info(f"Received unhandled or future callback action: {raw_data}")
        client.answer_callback_query(cb_id, text="Action received.")


def process_telegram_update(db: Session, client: TelegramClient, update: dict[str, Any]) -> None:
    """Route an incoming Telegram update to the appropriate handler."""
    if "callback_query" in update and update["callback_query"]:
        handle_telegram_callback_query(db, client, update["callback_query"])
        return

    is_edited = "edited_message" in update and update["edited_message"] is not None
    message = update.get("message") or update.get("edited_message") or update.get("channel_post")
    if not message:
        return

    # If this is an edited message, check if a podcast job already exists for it to prevent duplicate synthesis
    if is_edited:
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        msg_id = message.get("message_id")
        if chat_id and msg_id:
            existing_job = (
                db.query(PodcastJob)
                .filter(
                    PodcastJob.transport == "telegram",
                    PodcastJob.telegram_chat_id == chat_id,
                    PodcastJob.telegram_message_id == msg_id,
                )
                .first()
            )
            if existing_job:
                logger.info(
                    f"Ignoring edited_message for existing job '{existing_job.id}' (chat_id={chat_id}, message_id={msg_id})"
                )
                return

    text = message.get("text") or message.get("caption") or ""

    # Command detection
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        raw_cmd = parts[0][1:].lower()
        cmd = raw_cmd.split("@")[0]  # Remove bot username if present, e.g. /start@herald_bot
        args = parts[1] if len(parts) > 1 else ""
        handle_telegram_command(db, client, message, cmd, args)
    else:
        handle_telegram_content_message(db, client, message)


def sweep_unpresented_approval_cards(db: Session, client: TelegramClient) -> int:
    """
    Find AWAITING_APPROVAL jobs whose Telegram approval card was not successfully sent yet,
    and attempt delivery with bounded retries.
    """
    now = datetime.now(UTC)
    unpresented_jobs = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.transport == "telegram",
            PodcastJob.status == JobState.AWAITING_APPROVAL.value,
            PodcastJob.telegram_approval_message_id.is_(None),
            PodcastJob.telegram_chat_id.isnot(None),
            PodcastJob.attempt_count < 3,
        )
        .order_by(PodcastJob.created_at.asc())
        .limit(5)
        .all()
    )

    delivered = 0
    for job in unpresented_jobs:
        try:
            eta_info = calculate_job_eta(db, job)
            app_text, reply_markup = format_approval(job, job.script_json, eta_info)
            reply_id = int(job.telegram_message_id) if job.telegram_message_id else None
            sent_msg = client.send_message(
                chat_id=job.telegram_chat_id,
                text=app_text,
                reply_markup=reply_markup,
                reply_to_message_id=reply_id,
                parse_mode="HTML",
            )
            if sent_msg and isinstance(sent_msg, dict) and sent_msg.get("message_id"):
                job.telegram_approval_message_id = sent_msg["message_id"]
                job.approval_requested_at = now
                db.commit()
                delivered += 1
            else:
                job.attempt_count = (job.attempt_count or 0) + 1
                db.commit()
                logger.warning(
                    f"Approval card presentation returned no message_id for job '{job.id}' (attempt {job.attempt_count})"
                )
        except Exception as e:
            job.attempt_count = (job.attempt_count or 0) + 1
            db.commit()
            logger.warning(f"Failed retry to deliver approval card for job '{job.id}' (attempt {job.attempt_count}): {e}")
    return delivered


# Backward compatibility aliases
handle_telegram_message = handle_telegram_content_message


def run_telegram_bot(poll_interval: float = 1.0) -> None:
    """
    Main long-polling runner loop for the Herald Telegram bot daemon.
    Durably tracks update offset in PostgreSQL and quarantines poison updates after 3 failed attempts.
    """
    client = TelegramClient()
    logger.info("Starting Telegram Bot long-polling daemon...")

    # Load durable offset at startup
    with SessionLocal() as db:
        poll_state = db.query(TelegramPollState).first()
        if not poll_state:
            poll_state = TelegramPollState(last_processed_update_id=0)
            db.add(poll_state)
            db.commit()
            db.refresh(poll_state)
        last_offset = poll_state.last_processed_update_id

    while True:
        try:
            # 1. Deliver ready audio files and retry unpresented approval cards
            with SessionLocal() as db:
                deliver_pending_telegram_jobs(db, client)
                sweep_unpresented_approval_cards(db, client)

            # 2. Poll for new Telegram updates sequentially
            updates = client.get_updates(offset=last_offset + 1, limit=50, timeout=10)
            if updates:
                # Sort updates by update_id to ensure sequential processing
                sorted_updates = sorted(updates, key=lambda u: u.get("update_id", 0))

                for update in sorted_updates:
                    update_id = update.get("update_id")
                    if not update_id:
                        continue

                    with SessionLocal() as db:
                        try:
                            process_telegram_update(db, client, update)

                            # Persist durable offset only after successful processing
                            p_state = db.query(TelegramPollState).first()
                            if p_state:
                                p_state.last_processed_update_id = update_id
                                db.commit()
                            last_offset = update_id

                        except Exception as e:
                            logger.exception(f"Unexpected failure processing Telegram update {update_id}")
                            db.rollback()

                            # Track failed update attempt durably
                            fail_rec = db.query(TelegramUpdateFailure).filter_by(update_id=update_id).first()
                            if not fail_rec:
                                fail_rec = TelegramUpdateFailure(
                                    update_id=update_id,
                                    attempt_count=1,
                                    last_error=str(e)[:500],
                                )
                                db.add(fail_rec)
                            else:
                                fail_rec.attempt_count += 1
                                fail_rec.last_error = str(e)[:500]

                            if fail_rec.attempt_count >= 3:
                                fail_rec.is_dead_lettered = True
                                logger.error(
                                    f"Update {update_id} failed {fail_rec.attempt_count} times. "
                                    "Dead-lettering update and advancing polling offset."
                                )
                                p_state = db.query(TelegramPollState).first()
                                if p_state:
                                    p_state.last_processed_update_id = update_id
                                    db.commit()
                                last_offset = update_id
                            else:
                                db.commit()
                                # Break out of inner loop to retry after interval without advancing offset
                                break

        except Exception as e:
            logger.error(f"Error in Telegram bot loop: {e}")

        time.sleep(poll_interval)


run_telegram_bot_loop = run_telegram_bot
