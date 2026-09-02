import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from herald.config import settings
from herald.db.models import JobState, PodcastJob
from herald.db.state_machine import transition_job_state
from herald.services.performance_metrics import record_stage_metric
from herald.telegram.client import TelegramAPIError, TelegramClient

logger = logging.getLogger("herald.telegram.delivery")


def deliver_pending_telegram_jobs(db: Session, client: TelegramClient) -> int:
    """
    Find AUDIO_READY or retryable FAILED_RETRYABLE jobs originating from Telegram and deliver completed MP3.
    Uses atomic row locking (FOR UPDATE SKIP LOCKED) to prevent concurrent send duplication.
    Returns number of jobs processed.
    """
    if not client.is_configured:
        return 0

    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(minutes=15)

    eligible_filter = and_(
        PodcastJob.transport == "telegram",
        PodcastJob.telegram_chat_id.isnot(None),
        or_(
            PodcastJob.status == JobState.AUDIO_READY.value,
            and_(
                PodcastJob.status == JobState.FAILED_RETRYABLE.value,
                PodcastJob.failed_stage == "TELEGRAM_DELIVERY",
                or_(
                    PodcastJob.next_retry_at.is_(None),
                    PodcastJob.next_retry_at <= now,
                ),
            ),
            and_(
                PodcastJob.status == JobState.DELIVERING.value,
                PodcastJob.last_heartbeat_at <= stale_cutoff,
            ),
        ),
    )

    # Claim jobs atomically with row lock
    jobs = (
        db.query(PodcastJob)
        .filter(eligible_filter)
        .order_by(PodcastJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(10)
        .all()
    )

    if not jobs:
        return 0

    delivered_count = 0
    for job in jobs:
        # Move immediately to DELIVERING with fresh heartbeat
        if job.status != JobState.DELIVERING.value:
            transition_job_state(db, job, JobState.DELIVERING.value, component="telegram-delivery")
        job.last_heartbeat_at = now
        job.claimed_at = now
        db.commit()

        try:
            delivered = deliver_single_job(db, job, client)
            if delivered:
                delivered_count += 1
        except Exception as e:
            logger.error(f"Error delivering Telegram audio for job '{job.id}': {e}")

    return delivered_count


def deliver_single_job(db: Session, job: PodcastJob, client: TelegramClient) -> bool:
    """Deliver a claimed job to its Telegram chat with retry support and size safety."""
    t0 = datetime.now(UTC)
    chat_id = job.telegram_chat_id
    if not chat_id:
        return False

    script_obj = job.script_json or {}
    ep_title = script_obj.get("episode_title") or job.custom_title or "Herald Episode"
    mode_str = (job.request_mode or "standard").capitalize()
    if job.request_mode == "research" and job.research_depth:
        mode_str += f" ({job.research_depth.capitalize()})"

    audio_path_str = job.local_audio_path
    if not audio_path_str or not Path(audio_path_str).exists():
        err_msg = f"Audio file not found at '{audio_path_str}'"
        logger.error(f"Delivery failed for job '{job.id}': {err_msg}")
        job.error_code = "AUDIO_FILE_MISSING"
        job.error_detail = err_msg
        transition_job_state(
            db, job, JobState.FAILED_FINAL.value, component="telegram-delivery", message=err_msg
        )
        client.send_message(
            chat_id=chat_id,
            text=f"⚠️ Failed to deliver podcast: audio file is missing on server.\nJob ID: {job.id[:8]}",
        )
        return False

    audio_path = Path(audio_path_str)
    file_size_bytes = audio_path.stat().st_size
    max_bytes = getattr(settings, "TELEGRAM_MAX_AUDIO_BYTES", 50 * 1024 * 1024)

    # Format delivery caption
    dur_secs = job.audio_duration_seconds or 0
    dur_str = f"{dur_secs // 60}m {dur_secs % 60}s" if dur_secs >= 60 else f"{dur_secs}s"
    source_line = f"\nSource: {job.source_url}" if job.source_url else ""
    caption = f"🎙 {ep_title}\n⏱ {dur_str} | Mode: {mode_str}{source_line}"

    # Handle oversized audio
    if file_size_bytes > max_bytes:
        size_mb = file_size_bytes / (1024 * 1024)
        max_mb = max_bytes / (1024 * 1024)
        warn_msg = (
            f"⚠️ <b>Podcast Rendered</b>: {ep_title}\n\n"
            f"Your episode was generated successfully ({size_mb:.1f} MB), but exceeds this Herald "
            f"instance's Telegram delivery limit ({max_mb:.0f} MB).\n\n"
            f"The file has been retained locally for administrator recovery.\nJob ID: <code>{job.id[:8]}</code>"
        )
        client.send_message(chat_id=chat_id, text=warn_msg, parse_mode="HTML")
        job.error_code = "TELEGRAM_AUDIO_OVERSIZED"
        job.error_detail = f"Audio size {size_mb:.1f} MB exceeds limit {max_mb:.0f} MB"
        transition_job_state(
            db,
            job,
            JobState.FAILED_FINAL.value,
            component="telegram-delivery",
            message=f"Audio file ({size_mb:.1f} MB) exceeded Telegram limit ({max_mb:.0f} MB)",
        )
        return False

    reply_id = int(job.telegram_message_id) if job.telegram_message_id else None

    # Upload MP3 to Telegram
    try:
        client.send_audio(
            chat_id=chat_id,
            audio_path=audio_path,
            caption=caption,
            title=ep_title,
            performer="Herald",
            duration=dur_secs,
            reply_to_message_id=reply_id,
        )
    except TelegramAPIError as e:
        logger.error(f"Telegram sendAudio failed for job '{job.id}': {e}")
        # Retry with send_document fallback if audio upload failed
        try:
            client.send_document(
                chat_id=chat_id,
                document_path=audio_path,
                caption=caption,
                reply_to_message_id=reply_id,
            )
        except Exception as de:
            now = datetime.now(UTC)
            job.delivery_attempt_count = (job.delivery_attempt_count or 0) + 1
            job.failed_stage = "TELEGRAM_DELIVERY"
            job.error_code = "TELEGRAM_DELIVERY_FAILED"
            job.error_detail = str(de)[:500]

            if job.delivery_attempt_count >= 3:
                transition_job_state(
                    db,
                    job,
                    JobState.FAILED_FINAL.value,
                    component="telegram-delivery",
                    message=f"Telegram delivery failed after {job.delivery_attempt_count} attempts: {de}",
                )
                client.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Podcast delivery failed permanently after multiple attempts.\nJob ID: {job.id[:8]}",
                )
            else:
                backoff_secs = 15 * (2 ** (job.delivery_attempt_count - 1))
                job.next_retry_at = now + timedelta(seconds=backoff_secs)
                transition_job_state(
                    db,
                    job,
                    JobState.FAILED_RETRYABLE.value,
                    component="telegram-delivery",
                    message=f"Telegram delivery failed (attempt {job.delivery_attempt_count}, retrying in {backoff_secs}s): {de}",
                )
                job.failed_stage = "TELEGRAM_DELIVERY"
            db.commit()
            return False

    job.delivered_at = datetime.now(UTC)
    transition_job_state(
        db,
        job,
        JobState.COMPLETE.value,
        component="telegram-delivery",
        message="Delivered podcast audio successfully to Telegram chat",
    )

    record_stage_metric(
        job_id=job.id,
        stage="TELEGRAM_DELIVERY",
        started_at=t0,
        finished_at=datetime.now(UTC),
        status="success",
        output_bytes=file_size_bytes,
        audio_duration_ms=dur_secs * 1000,
    )

    logger.info(f"Delivered completed MP3 for job '{job.id}' to Telegram chat '{chat_id}'")
    return True
