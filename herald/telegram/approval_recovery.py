"""
Approval and rerun confirmation delivery recovery service for Herald Telegram.
Periodically sweeps for jobs in AWAITING_APPROVAL or AWAITING_RERUN_CONFIRMATION
whose Telegram cards failed initial delivery or have not been confirmed sent,
and re-delivers them with bounded attempt counts.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from herald.db.models import JobState, PodcastJob
from herald.services.eta_calculator import calculate_job_eta
from herald.telegram.client import TelegramClient
from herald.telegram.formatters import format_approval, format_rerun_confirmation

logger = logging.getLogger("herald.telegram.approval_recovery")


def sweep_unpresented_approval_cards(db: Session, client: TelegramClient) -> int:
    """
    Find AWAITING_APPROVAL and AWAITING_RERUN_CONFIRMATION jobs whose Telegram
    card was not successfully sent yet, and attempt delivery with bounded retries.
    """
    now = datetime.now(UTC)
    unpresented_jobs = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.transport == "telegram",
            PodcastJob.status.in_([
                JobState.AWAITING_APPROVAL.value,
                JobState.AWAITING_RERUN_CONFIRMATION.value,
            ]),
            PodcastJob.telegram_approval_message_id.is_(None),
            PodcastJob.telegram_chat_id.isnot(None),
            PodcastJob.attempt_count < 3,
        )
        .order_by(PodcastJob.created_at.asc())
        .limit(10)
        .all()
    )

    delivered = 0
    for job in unpresented_jobs:
        try:
            reply_id = int(job.telegram_message_id) if job.telegram_message_id else None

            if job.status == JobState.AWAITING_APPROVAL.value:
                eta_info = calculate_job_eta(db, job)
                card_text, reply_markup = format_approval(job, job.script_json, eta_info)
            elif job.status == JobState.AWAITING_RERUN_CONFIRMATION.value:
                prior_job = None
                if job.rerun_of_job_id:
                    prior_job = db.query(PodcastJob).filter_by(id=job.rerun_of_job_id).first()
                if not prior_job and job.source_hash:
                    # Fallback to finding prior content candidate by source_hash
                    prior_job = (
                        db.query(PodcastJob)
                        .filter(
                            PodcastJob.source_hash == job.source_hash,
                            PodcastJob.id != job.id,
                        )
                        .first()
                    )
                if not prior_job:
                    logger.warning(
                        f"Cannot render rerun confirmation for job '{job.id}': prior job not found."
                    )
                    job.attempt_count = (job.attempt_count or 0) + 1
                    db.commit()
                    continue

                card_text, reply_markup = format_rerun_confirmation(
                    new_job=job,
                    prior_job=prior_job,
                )
            else:
                continue

            sent_msg = client.send_message(
                chat_id=job.telegram_chat_id,
                text=card_text,
                reply_markup=reply_markup,
                reply_to_message_id=reply_id,
                parse_mode="HTML",
            )
            if sent_msg and isinstance(sent_msg, dict) and sent_msg.get("message_id"):
                job.telegram_approval_message_id = sent_msg["message_id"]
                job.approval_requested_at = now
                db.commit()
                delivered += 1
                logger.info(
                    f"Successfully delivered recovered {job.status} card for job '{job.id}' (msg_id: {sent_msg['message_id']})"
                )
            else:
                job.attempt_count = (job.attempt_count or 0) + 1
                db.commit()
                logger.warning(
                    f"Card presentation returned no message_id for job '{job.id}' (attempt {job.attempt_count})"
                )
        except Exception as e:
            job.attempt_count = (job.attempt_count or 0) + 1
            db.commit()
            logger.warning(
                f"Failed retry to deliver card for job '{job.id}' in state {job.status} (attempt {job.attempt_count}): {e}"
            )

    return delivered
