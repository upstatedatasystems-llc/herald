"""
Safe, tenant-isolated job resolver for Telegram user commands and callbacks.
"""

import logging

from sqlalchemy.orm import Session

from herald.db.models import JobState, PodcastJob

logger = logging.getLogger("herald.telegram.resolver")


def resolve_user_job(
    db: Session,
    user_id: int | str,
    chat_id: int | str,
    query_or_id: str | None = None,
) -> PodcastJob | None:
    """
    Safely resolve a PodcastJob belonging to the specific Telegram user and chat context.
    - If query_or_id is provided, matches exact UUID or prefix (min 4 chars).
    - If query_or_id is omitted/empty, returns the user's most recent COMPLETE podcast job.
    - Strictly enforces tenant isolation: telegram_user_id and telegram_chat_id must match.
    """
    try:
        uid_int = int(user_id)
        cid_int = int(chat_id)
    except (ValueError, TypeError):
        return None

    clean_query = (query_or_id or "").strip()

    if clean_query:
        # 1. Exact UUID match
        job = (
            db.query(PodcastJob)
            .filter(
                PodcastJob.transport == "telegram",
                PodcastJob.telegram_user_id == uid_int,
                PodcastJob.telegram_chat_id == cid_int,
                PodcastJob.id == clean_query,
            )
            .first()
        )
        if job:
            return job

        # 2. Prefix match (minimum 4 characters)
        if len(clean_query) >= 4:
            job = (
                db.query(PodcastJob)
                .filter(
                    PodcastJob.transport == "telegram",
                    PodcastJob.telegram_user_id == uid_int,
                    PodcastJob.telegram_chat_id == cid_int,
                    PodcastJob.id.startswith(clean_query),
                )
                .order_by(PodcastJob.created_at.desc())
                .first()
            )
            if job:
                return job

        return None

    # 3. Default: most recent COMPLETE job for this user
    return (
        db.query(PodcastJob)
        .filter(
            PodcastJob.transport == "telegram",
            PodcastJob.telegram_user_id == uid_int,
            PodcastJob.telegram_chat_id == cid_int,
            PodcastJob.status == JobState.COMPLETE.value,
        )
        .order_by(PodcastJob.completed_at.desc(), PodcastJob.created_at.desc())
        .first()
    )
