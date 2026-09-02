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
from herald.db.models import JobState, PodcastJob, RequestMode
from herald.extraction.email_parser import (
    BASE_SUBJECT_PATTERNS,
    URL_REGEX,
    parse_directives,
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
    Supports inline mode directives (e.g. 'literal', 'brief', 'standard', 'research [depth]', 'podcast: <mode>'),
    top directives (Voice:, Speed:, Title:, Research:), and URL extraction.
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

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matched_mode = None
    matched_depth = None
    voice = None
    speed = None
    title = None
    chunk_chars = 500
    verify = False
    remaining_lines = []

    allowed_voices = settings.get_allowed_voices_list()

    # 1. Inspect lines for mode or directives
    for line in lines:
        lower_line = line.lower()

        # Check for mode tokens
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
                chunk_chars = int(val_str)
            continue

        # Check for explicit directives
        if lower_line.startswith("voice:"):
            v_val = line.split(":", 1)[1].strip().lower()
            if v_val in allowed_voices:
                voice = v_val
            continue
        elif lower_line.startswith("speed:"):
            s_val = line.split(":", 1)[1].strip()
            try:
                s_float = float(s_val)
                if settings.MIN_SPEED <= s_float <= settings.MAX_SPEED:
                    speed = s_float
            except ValueError:
                pass
            continue
        elif lower_line.startswith("title:"):
            t_val = line.split(":", 1)[1].strip()
            if t_val:
                title = t_val
            continue
        elif lower_line.startswith("research:"):
            r_val = line.split(":", 1)[1].strip().lower()
            if r_val in ("low", "medium", "high"):
                matched_depth = r_val
            continue

        remaining_lines.append(line)

    body = "\n".join(remaining_lines)

    # 2. Detect URL in clean body
    detected_url = None
    url_match = URL_REGEX.search(body)
    if url_match:
        detected_url = url_match.group(0)

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
    """Route and process Telegram bot slash commands."""
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    username = message.get("from", {}).get("username")
    first_name = message.get("from", {}).get("first_name")
    msg_id = message.get("message_id")

    if not chat_id or not user_id:
        return

    cmd = command.lower()

    # /pair command handler (accessible before authorization)
    if cmd in ("/pair", "pair"):
        code = args.strip()
        if not code:
            client.send_message(
                chat_id=chat_id,
                text="Please provide your pairing code:\nUsage: <code>/pair 123456</code>",
                parse_mode="HTML",
                reply_to_message_id=msg_id,
            )
            return

        success, reply_text = verify_and_claim_pairing_code(
            db=db,
            code=code,
            user_id=user_id,
            chat_id=chat_id,
            username=username,
            first_name=first_name,
        )
        client.send_message(
            chat_id=chat_id,
            text=f"<b>Herald Pairing</b>\n\n{reply_text}",
            parse_mode="HTML",
            reply_to_message_id=msg_id,
        )
        return

    # Check authorization for all other commands
    if not is_user_authorized(db, user_id):
        client.send_message(
            chat_id=chat_id,
            text=(
                "⛔ <b>Access Denied</b>\n\n"
                "This Herald instance is private.\n"
                "To pair your Telegram account as the owner, run:\n"
                "<code>/pair &lt;code&gt;</code>\n"
                "using the pairing code displayed in the server startup console."
            ),
            parse_mode="HTML",
            reply_to_message_id=msg_id,
        )
        return

    if cmd in ("/start", "start"):
        client.send_message(
            chat_id=chat_id,
            text=(
                "🎙 <b>Welcome to Herald!</b>\n\n"
                "Herald transforms articles, documents, and notes into audio podcasts.\n\n"
                "<b>Quick Start:</b>\n"
                "• Send an article URL: <code>https://example.com/article</code>\n"
                "• Paste text or forward a message\n"
                "• Add a mode prefix (e.g. <code>literal</code>, <code>standard</code>, <code>research</code>)\n\n"
                "Type /help for full instructions and options."
            ),
            parse_mode="HTML",
            reply_to_message_id=msg_id,
        )

    elif cmd in ("/help", "help"):
        client.send_message(
            chat_id=chat_id,
            text=(
                "📖 <b>Herald Telegram Guide</b>\n\n"
                "<b>Modes:</b>\n"
                "• <code>literal</code>: Deterministic direct reading (zero AI API calls)\n"
                "• <code>standard</code>: AI-narrated podcast episode\n"
                "• <code>brief</code>: Concise summary narration\n"
                "• <code>research [low|med|high]</code>: Grounded deep-dive analysis\n\n"
                "<b>Directives (optional top lines):</b>\n"
                "• <code>Voice: af_bella</code> (af_heart, af_bella, af_sarah, am_adam, am_michael)\n"
                "• <code>Speed: 1.1</code> (0.8 to 1.2)\n"
                "• <code>Title: My Custom Title</code>\n\n"
                "<b>Commands:</b>\n"
                "/status — Runtime, TTS, and AI health check\n"
                "/ai-check — Test AI provider connection\n"
                "/queue — View pending audio jobs\n"
                "/settings — Current Herald configuration\n"
                "/readme — Send full documentation\n"
                "/help — Show this help message"
            ),
            parse_mode="HTML",
            reply_to_message_id=msg_id,
        )

    elif cmd in ("/readme", "readme"):
        readme_path = Path("README.md")
        if readme_path.exists():
            try:
                client.send_document(
                    chat_id=chat_id,
                    document_path=readme_path,
                    caption="📄 Herald Documentation (README.md)",
                    reply_to_message_id=msg_id,
                )
            except Exception as e:
                logger.warning(f"send_document failed for README.md: {e}")
                # Fallback to sending text if document upload fails
                readme_text = readme_path.read_text(encoding="utf-8")[:3000]
                client.send_message(
                    chat_id=chat_id,
                    text=f"<b>Herald README</b>\n\n<pre>{readme_text}</pre>",
                    parse_mode="HTML",
                    reply_to_message_id=msg_id,
                )
        else:
            client.send_message(
                chat_id=chat_id,
                text="README.md not found on server.",
                reply_to_message_id=msg_id,
            )

    elif cmd in ("/status", "status"):
        # Kokoro health check
        tts_ok = False
        try:
            k_res = KokoroClient().health_check()
            tts_ok = bool(isinstance(k_res, dict) and k_res.get("healthy"))
        except Exception:
            tts_ok = False

        # AI Provider health check
        ai_provider = get_ai_provider()
        ai_name = ai_provider.provider_name if ai_provider else "None"
        ai_conn_str = "Not configured"
        if ai_provider:
            try:
                check_res = ai_provider.check_connection(timeout_seconds=4.0)
                ai_conn_str = "OK" if check_res.get("connected") else f"FAILED ({check_res.get('error')})"
            except Exception as e:
                ai_conn_str = f"Error: {e}"

        # Queue count
        queued_count = (
            db.query(PodcastJob)
            .filter(
                PodcastJob.status.in_([
                    JobState.RECEIVED.value,
                    JobState.VALIDATING.value,
                    JobState.EXTRACTING.value,
                    JobState.SCRIPTING.value,
                    JobState.QUEUED_TTS.value,
                    JobState.SYNTHESIZING.value,
                    JobState.ENCODING.value,
                    JobState.AUDIO_READY.value,
                    JobState.DELIVERING.value,
                ])
            )
            .count()
        )

        active_job = (
            db.query(PodcastJob)
            .filter(PodcastJob.status.in_([JobState.SYNTHESIZING.value, JobState.ENCODING.value]))
            .first()
        )
        active_str = f"Job {active_job.id[:8]} ({active_job.status})" if active_job else "None"

        # Disk space
        work_dir = Path(settings.HERALD_WORK_DIR)
        free_mb = check_free_disk_mb(work_dir)
        free_gb = free_mb / 1024.0

        # Uptime
        uptime_delta = datetime.now(UTC) - _START_TIME
        days = uptime_delta.days
        hours = uptime_delta.seconds // 3600
        mins = (uptime_delta.seconds % 3600) // 60
        uptime_str = f"{days}d {hours}h {mins}m" if days > 0 else f"{hours}h {mins}m"

        avail_script = "AI (Gemini) + Literal" if settings.is_ai_configured() else "Literal only"

        status_text = (
            f"<b>Herald Status</b>\n\n"
            f"TTS: {'Ready' if tts_ok else 'Unavailable'}\n"
            f"AI Provider: {ai_name}\n"
            f"AI Connection: {ai_conn_str}\n"
            f"Available Scripting: {avail_script}\n"
            f"Queue: {queued_count}\n"
            f"Active Job: {active_str}\n"
            f"Disk: {free_gb:.1f} GB free\n"
            f"Uptime: {uptime_str}"
        )
        client.send_message(chat_id=chat_id, text=status_text, parse_mode="HTML", reply_to_message_id=msg_id)

    elif cmd in ("/ai-check", "ai-check"):
        ai_provider = get_ai_provider()
        if not ai_provider or not settings.is_ai_configured():
            client.send_message(
                chat_id=chat_id,
                text="<b>AI Health Check</b>\n\nAI Provider: Not configured\nMode: Literal only",
                parse_mode="HTML",
                reply_to_message_id=msg_id,
            )
            return

        try:
            check_res = ai_provider.check_connection(timeout_seconds=5.0)
            conn_ok = check_res.get("connected", False)
            model_name = check_res.get("model", settings.GEMINI_MODEL)
            if conn_ok:
                rep = (
                    f"<b>AI Health Check</b>\n\n"
                    f"AI Provider: {ai_provider.provider_name}\n"
                    f"Configuration: Present\n"
                    f"Connection: OK\n"
                    f"Model: <code>{model_name}</code>"
                )
            else:
                err = check_res.get("error", "Unknown error")
                rep = (
                    f"<b>AI Health Check</b>\n\n"
                    f"AI Provider: {ai_provider.provider_name}\n"
                    f"Configuration: Present\n"
                    f"Connection: FAILED\n"
                    f"Error: <code>{err}</code>\n"
                    f"Model: <code>{model_name}</code>"
                )
            client.send_message(chat_id=chat_id, text=rep, parse_mode="HTML", reply_to_message_id=msg_id)
        except Exception as e:
            client.send_message(
                chat_id=chat_id,
                text=f"<b>AI Health Check</b>\n\nAI Provider: {ai_provider.provider_name}\nConnection: FAILED\nError: <code>{e}</code>",
                parse_mode="HTML",
                reply_to_message_id=msg_id,
            )

    elif cmd in ("/queue", "queue"):
        jobs = (
            db.query(PodcastJob)
            .filter(
                PodcastJob.status.in_([
                    JobState.RECEIVED.value,
                    JobState.VALIDATING.value,
                    JobState.EXTRACTING.value,
                    JobState.SCRIPTING.value,
                    JobState.QUEUED_TTS.value,
                    JobState.SYNTHESIZING.value,
                    JobState.ENCODING.value,
                    JobState.AUDIO_READY.value,
                    JobState.DELIVERING.value,
                ])
            )
            .order_by(PodcastJob.created_at.asc())
            .limit(10)
            .all()
        )
        if not jobs:
            client.send_message(
                chat_id=chat_id,
                text="Queue is currently empty. No active or pending podcast jobs.",
                reply_to_message_id=msg_id,
            )
            return

        lines = ["<b>Current Queue:</b>"]
        for j in jobs:
            ep = (j.script_json or {}).get("episode_title") or j.custom_title or "Episode"
            lines.append(f"• <code>{j.id[:8]}</code> | <b>{j.status}</b> | {j.request_mode} | {ep}")

        client.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML", reply_to_message_id=msg_id)

    elif cmd in ("/settings", "settings"):
        default_mode = settings.get_default_mode()
        voice = settings.KOKORO_VOICE
        speed = settings.KOKORO_SPEED
        ai_prov = settings.AI_PROVIDER if settings.is_ai_configured() else "None (Literal only)"
        retention = settings.LOCAL_COMPLETE_RETENTION_HOURS

        settings_text = (
            f"<b>Herald Settings</b>\n\n"
            f"• Default Mode: <code>{default_mode}</code>\n"
            f"• Kokoro Voice: <code>{voice}</code>\n"
            f"• Narration Speed: <code>{speed}x</code>\n"
            f"• AI Provider: <code>{ai_prov}</code>\n"
            f"• Audio Retention: <code>{retention} hours</code>\n"
            f"• Allowed Voices: <code>{settings.ALLOWED_VOICES}</code>"
        )
        client.send_message(chat_id=chat_id, text=settings_text, parse_mode="HTML", reply_to_message_id=msg_id)

    else:
        client.send_message(
            chat_id=chat_id,
            text=f"Unknown command: <code>{command}</code>. Send /help for available commands.",
            parse_mode="HTML",
            reply_to_message_id=msg_id,
        )


def handle_telegram_message(
    db: Session,
    client: TelegramClient,
    message: dict[str, Any],
) -> None:
    """Process an incoming non-command Telegram message containing text or URLs."""
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    msg_id = message.get("message_id")
    raw_text = message.get("text") or message.get("caption") or ""

    if not chat_id or not user_id:
        return

    # Check authorization
    if not is_user_authorized(db, user_id):
        client.send_message(
            chat_id=chat_id,
            text=(
                "⛔ <b>Access Denied</b>\n\n"
                "This Herald instance is private.\n"
                "To pair your Telegram account as the owner, run:\n"
                "<code>/pair &lt;code&gt;</code>\n"
                "using the pairing code displayed in the server startup console."
            ),
            parse_mode="HTML",
            reply_to_message_id=msg_id,
        )
        return

    if not raw_text.strip():
        client.send_message(
            chat_id=chat_id,
            text="Please send an article URL, pasted text, or forwarded message.",
            reply_to_message_id=msg_id,
        )
        return

    # Send typing status
    try:
        client.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    parsed = parse_telegram_message_directives(raw_text)

    # Build generic HeraldRequest
    req = HeraldRequest(
        source_text=parsed["text"] if not parsed["url"] else None,
        source_url=parsed["url"],
        request_mode=parsed["mode"],
        research_depth=parsed["research_depth"],
        requester_identity=f"telegram:{user_id}",
        delivery_target=str(chat_id),
        custom_voice=parsed["voice"],
        custom_speed=parsed["speed"],
        custom_title=parsed["title"],
        tts_chunk_chars=parsed["chunk_chars"],
        verify_final_script=parsed["verify"],
        transport="telegram",
        transport_message_id=str(msg_id),
    )

    resp: HeraldResponse = process_herald_request(db, req)

    if resp.status == JobState.FAILED_FINAL.value:
        client.send_message(
            chat_id=chat_id,
            text=f"⚠️ <b>Request Rejected</b>\n\n{resp.message}",
            parse_mode="HTML",
            reply_to_message_id=msg_id,
        )
    elif resp.is_duplicate:
        client.send_message(
            chat_id=chat_id,
            text=(
                f"ℹ️ <b>Already Processed</b>\n\n"
                f"Job <code>{resp.job_id[:8]}</code> was previously accepted.\n"
                f"Status: <b>{resp.status}</b>"
            ),
            parse_mode="HTML",
            reply_to_message_id=msg_id,
        )
    else:
        mode_label = resp.request_mode.capitalize()
        if parsed.get("research_depth"):
            mode_label += f" {parsed['research_depth'].capitalize()}"

        client.send_message(
            chat_id=chat_id,
            text=(
                f"<b>Accepted.</b>\n"
                f"Mode: <b>{mode_label}</b>\n"
                f"Job: <code>{resp.job_id[:8]}</code>\n"
                f"Queued."
            ),
            parse_mode="HTML",
            reply_to_message_id=msg_id,
        )


def process_telegram_update(
    db: Session,
    client: TelegramClient,
    update: dict[str, Any],
) -> None:
    """Parse and route a single Telegram update dict."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        return

    # Check for command prefix
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        # Strip bot username suffix if present, e.g. /start@MyBot -> /start
        if "@" in cmd:
            cmd = cmd.split("@")[0]
        handle_telegram_command(db, client, message, cmd, args)
    else:
        handle_telegram_message(db, client, message)


def run_telegram_bot_loop(stop_event: Any = None) -> None:
    """
    Main Telegram polling daemon.
    Polls getUpdates, processes incoming messages/commands, and delivers completed audio.
    """
    client = TelegramClient()
    if not client.is_configured:
        logger.warning("Telegram bot token is not configured. Telegram bot service cannot start.")
        return

    logger.info("Starting Telegram Bot listener with outbound long polling...")

    # Validate bot connectivity on startup
    try:
        bot_info = client.get_me()
        bot_username = bot_info.get("username", "HeraldBot")
        logger.info(f"Connected to Telegram Bot: @{bot_username}")
    except Exception as e:
        logger.error(f"Failed to connect to Telegram Bot API: {e}")

    last_offset = None
    poll_timeout = settings.TELEGRAM_POLL_TIMEOUT_SECONDS

    while stop_event is None or not stop_event.is_set():
        try:
            db = SessionLocal()
            try:
                # 1. Check for completed Telegram audio jobs and deliver
                deliver_pending_telegram_jobs(db, client)

                # 2. Fetch new updates via long polling
                updates = client.get_updates(offset=last_offset, timeout=poll_timeout)
                for up in updates:
                    up_id = up.get("update_id")
                    if up_id is not None:
                        last_offset = up_id + 1
                    try:
                        process_telegram_update(db, client, up)
                    except Exception as ue:
                        logger.error(f"Error handling Telegram update {up_id}: {ue}")

                # 3. Deliver any jobs that became ready during update processing
                deliver_pending_telegram_jobs(db, client)
            finally:
                db.close()

        except Exception as e:
            logger.error(f"Error in Telegram bot loop: {e}")
            time.sleep(3)
