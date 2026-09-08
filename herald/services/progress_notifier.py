"""
TTS progress milestone notification service.
Sends truthful first-chunk progress milestone notification for Telegram jobs
with atomic CAS claiming, accurate provider/TTS attribution, and extended ETA v2.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from herald.db.models import PodcastJob
from herald.services.diagnostic_recorder import record_job_diagnostic_event
from herald.services.eta_calculator import calculate_job_eta
from herald.telegram.client import TelegramClient
from herald.telegram.formatters import format_first_chunk_progress

logger = logging.getLogger("herald.services.progress_notifier")


def notify_tts_chunk_progress(
    db: Session,
    job: PodcastJob,
    chunk_index: int,
    total_chunks: int,
    chunk_audio_duration_s: float,
    chunk_synthesis_duration_s: float,
    telegram_client: TelegramClient | None = None,
) -> bool:
    """
    Handle progress milestone notification when a TTS chunk completes.
    Strictly fires only once for chunk_index == 1 using an atomic CAS DB claim with retry lease.
    Returns True if first-chunk milestone notification was sent, False otherwise.
    """
    if chunk_index != 1:
        return False

    if job.transport != "telegram" or not job.telegram_chat_id:
        return False

    # Atomic CAS claim with 30s lease expiration to guarantee safe retry if send fails
    now = datetime.now(UTC)
    lease_threshold = now - timedelta(seconds=30)
    updated_rows = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.id == job.id,
            PodcastJob.telegram_progress_message_id.is_(None),
            or_(
                PodcastJob.first_chunk_progress_claimed_at.is_(None),
                PodcastJob.first_chunk_progress_claimed_at < lease_threshold,
            ),
        )
        .update(
            {"first_chunk_progress_claimed_at": now},
            synchronize_session=False,
        )
    )
    db.commit()

    if updated_rows != 1:
        logger.debug(
            f"First-chunk progress milestone already claimed for job '{job.id}'. Skipping notification."
        )
        return False

    # Compute empirical first-chunk RTF
    c1_rtf = None
    if chunk_audio_duration_s > 0 and chunk_synthesis_duration_s > 0:
        c1_rtf = chunk_synthesis_duration_s / float(chunk_audio_duration_s)

    # Recalculate ETA v2 using first-chunk measured RTF
    eta_info = calculate_job_eta(db, job, measured_first_chunk_rtf=c1_rtf)
    eta_range = eta_info.get("estimated_completion_range") or "approximately 2–4 minutes"

    # Format card with truthful AI & TTS attribution
    msg_text = format_first_chunk_progress(
        job=job,
        total_chunks=total_chunks,
        eta_range=eta_range,
    )

    client = telegram_client or TelegramClient()
    if not client.is_configured:
        logger.debug("Telegram client not configured; cannot deliver first-chunk milestone.")
        try:
            db.query(PodcastJob).filter(
                PodcastJob.id == job.id,
                PodcastJob.telegram_progress_message_id.is_(None),
            ).update(
                {"first_chunk_progress_claimed_at": None},
                synchronize_session=False,
            )
            db.commit()
        except Exception:
            db.rollback()
        return False

    try:
        sent_msg = client.send_message(
            chat_id=job.telegram_chat_id,
            text=msg_text,
            reply_to_message_id=job.telegram_message_id,
            parse_mode="HTML",
        )
        if sent_msg and isinstance(sent_msg, dict) and sent_msg.get("message_id"):
            progress_msg_id = sent_msg["message_id"]
            db.query(PodcastJob).filter(PodcastJob.id == job.id).update(
                {"telegram_progress_message_id": progress_msg_id},
                synchronize_session=False,
            )
            db.commit()
            db.refresh(job)

            record_job_diagnostic_event(
                job.id,
                "INFO",
                "tts",
                "FIRST_CHUNK_PROGRESS_SENT",
                f"Sent first-chunk progress milestone notification to Telegram chat {job.telegram_chat_id}",
                metadata={
                    "message_id": progress_msg_id,
                    "eta_range": eta_range,
                    "c1_rtf": c1_rtf,
                },
                db=db,
            )
            return True
        else:
            # Did not get valid message_id; clear claim for retry
            try:
                db.query(PodcastJob).filter(
                    PodcastJob.id == job.id,
                    PodcastJob.telegram_progress_message_id.is_(None),
                ).update(
                    {"first_chunk_progress_claimed_at": None},
                    synchronize_session=False,
                )
                db.commit()
            except Exception:
                db.rollback()
    except Exception as e:
        logger.warning(
            f"Non-fatal error delivering first-chunk milestone notification for job '{job.id}': {e}"
        )
        try:
            db.query(PodcastJob).filter(
                PodcastJob.id == job.id,
                PodcastJob.telegram_progress_message_id.is_(None),
            ).update(
                {"first_chunk_progress_claimed_at": None},
                synchronize_session=False,
            )
            db.commit()
        except Exception:
            db.rollback()

    return False
