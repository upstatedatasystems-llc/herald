import html
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from sqlalchemy.orm import Session

from herald.ai.factory import get_ai_provider
from herald.audio.ffmpeg_builder import check_free_disk_mb
from herald.config import settings
from herald.core.models import HeraldRequest, HeraldResponse
from herald.core.pipeline import process_herald_request
from herald.db.connection import SessionLocal
from herald.db.models import JobState, PodcastJob, RequestMode, TelegramPollState, TelegramUpdateFailure
from herald.extraction.email_parser import (
    BASE_SUBJECT_PATTERNS,
    URL_REGEX,
)
from herald.telegram.auth import (
    get_or_create_active_pairing_code,
    get_paired_owner,
    has_owner,
    is_user_authorized,
    verify_and_claim_pairing_code,
)
from herald.telegram.client import TelegramClient
from herald.telegram.delivery import deliver_pending_telegram_jobs
from herald.tts.kokoro_client import KokoroClient

logger = logging.getLogger("herald.telegram.bot")

_START_TIME = datetime.now(UTC)


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
            if stripped.startswith(("http://", "https://")) and not " " in stripped:
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
            client.send_message(
                chat_id=chat_id,
                text=f"✅ <b>{escaped_reply}</b>\n\nYou can now send article URLs or paste text to generate podcasts.",
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
        owner_name = html.escape(owner.first_name or owner.username or str(owner.telegram_user_id)) if owner else "Owner"
        default_mode = settings.get_default_mode()
        ai_prov = settings.AI_PROVIDER or "None (Literal only)"
        client.send_message(
            chat_id=chat_id,
            text=(
                f"🎙️ <b>Welcome to Herald!</b>\n\n"
                f"Owner: <b>{owner_name}</b>\n"
                f"Default Mode: <code>{html.escape(default_mode)}</code>\n"
                f"AI Provider: <code>{html.escape(ai_prov)}</code>\n\n"
                f"Send an article URL or paste text to generate a podcast.\n\n"
                f"Use /help to view available commands and directives."
            ),
            reply_to_message_id=msg_id,
            parse_mode="HTML",
        )

    elif cmd_clean == "help":
        help_text = (
            "📖 <b>Herald Usage Guide</b>\n\n"
            "<b>Ways to generate audio:</b>\n"
            "• Send an article URL (e.g. <code>https://example.com/article</code>)\n"
            "• Paste or forward an article, document, or newsletter\n\n"
            "<b>Modes (top of message):</b>\n"
            "• <code>literal</code> — Local deterministic reading (no AI required)\n"
            "• <code>brief</code> — Concise AI summary\n"
            "• <code>standard</code> — Full AI podcast narration\n"
            "• <code>research high</code> — Deep-dive grounded research podcast\n\n"
            "<b>Directives:</b>\n"
            "• <code>Voice: af_bella</code> (af_heart, af_bella, af_sarah, am_adam, am_michael)\n"
            "• <code>Speed: 1.1</code> (0.8 to 1.2)\n"
            "• <code>Title: Custom Title</code>\n\n"
            "<b>Commands:</b>\n"
            "/status — Live system health, AI status, and queue depth\n"
            "/ai-check — Fresh AI provider connection test\n"
            "/queue — Pending and processing jobs\n"
            "/settings — Configuration and defaults\n"
            "/readme — Project documentation"
        )
        client.send_message(chat_id=chat_id, text=help_text, reply_to_message_id=msg_id, parse_mode="HTML")

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

        # Queue counts
        active_count = (
            db.query(PodcastJob)
            .filter(PodcastJob.status.in_([
                JobState.RECEIVED.value,
                JobState.VALIDATING.value,
                JobState.EXTRACTING.value,
                JobState.SCRIPTING.value,
                JobState.QUEUED_TTS.value,
                JobState.SYNTHESIZING.value,
                JobState.ENCODING.value,
                JobState.DELIVERING.value,
            ]))
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

    elif cmd_clean == "ai-check":
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
        jobs = (
            db.query(PodcastJob)
            .filter(PodcastJob.status.notin_([JobState.COMPLETE.value, JobState.FAILED_FINAL.value, JobState.CANCELLED.value]))
            .order_by(PodcastJob.created_at.asc())
            .limit(10)
            .all()
        )
        if not jobs:
            client.send_message(chat_id=chat_id, text="📭 <b>Queue is currently empty.</b>", reply_to_message_id=msg_id, parse_mode="HTML")
            return

        lines = ["📋 <b>Active Podcast Queue:</b>\n"]
        for j in jobs:
            t = html.escape(j.custom_title or (j.script_json or {}).get("episode_title") or "Untitled")
            mode = html.escape(j.request_mode)
            st = html.escape(j.status)
            lines.append(f"• <b>{t}</b> [{mode}] — <code>{st}</code>")

        client.send_message(chat_id=chat_id, text="\n".join(lines), reply_to_message_id=msg_id, parse_mode="HTML")

    elif cmd_clean == "settings":
        settings_msg = (
            "⚙️ <b>Instance Settings:</b>\n\n"
            f"• <b>Default Mode:</b> <code>{html.escape(settings.get_default_mode())}</code>\n"
            f"• <b>Default Voice:</b> <code>{html.escape(settings.KOKORO_VOICE)}</code>\n"
            f"• <b>Default Speed:</b> <code>{settings.KOKORO_SPEED}x</code>\n"
            f"• <b>Max Audio Upload:</b> <code>{settings.TELEGRAM_MAX_AUDIO_BYTES / (1024*1024):.0f} MB</code>\n"
            f"• <b>AI Provider:</b> <code>{html.escape(settings.AI_PROVIDER or 'None')}</code>\n"
        )
        client.send_message(chat_id=chat_id, text=settings_msg, reply_to_message_id=msg_id, parse_mode="HTML")

    elif cmd_clean == "readme":
        readme_path = Path("README.md")
        if readme_path.exists():
            client.send_document(
                chat_id=chat_id,
                file_path=str(readme_path),
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

    req = HeraldRequest(
        transport="telegram",
        transport_message_id=msg_id,
        requester_identity=f"telegram:{user_id}",
        delivery_target=str(chat_id),
        mode=parsed["mode"],
        research_depth=parsed["research_depth"],
        source_url=parsed["url"],
        source_text=parsed["text"] if not parsed["url"] else None,
        custom_voice=parsed["voice"],
        custom_speed=parsed["speed"],
        custom_title=parsed["title"],
        tts_chunk_chars=parsed["chunk_chars"],
        verify_final_script=parsed["verify"],
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
        if response.status == JobState.COMPLETE.value:
            # Check if local MP3 is present on disk for immediate delivery
            existing_job = db.query(PodcastJob).filter_by(id=response.job_id).first()
            if existing_job and existing_job.local_audio_path and os.path.exists(existing_job.local_audio_path):
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

    # Intake acknowledgment
    client.send_message(
        chat_id=chat_id,
        text=(
            f"🎙️ <b>Podcast Queued!</b>\n\n"
            f"• <b>Title:</b> {title_escaped}\n"
            f"• <b>Format:</b> {mode_escaped}\n"
            f"• <b>Status:</b> <code>{html.escape(response.status)}</code>\n\n"
            f"Synthesizing audio now. You will receive the completed MP3 file shortly."
        ),
        reply_to_message_id=msg_id,
        parse_mode="HTML",
    )


def process_telegram_update(db: Session, client: TelegramClient, update: dict[str, Any]) -> None:
    """Route an incoming Telegram update to the appropriate handler."""
    message = update.get("message") or update.get("channel_post")
    if not message:
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
            # 1. Deliver any ready audio files to Telegram
            with SessionLocal() as db:
                deliver_pending_telegram_jobs(db, client)

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
