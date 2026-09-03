"""
Safe, tenant-isolated job resolver for Telegram user commands and callbacks.
"""

import logging
import re

from sqlalchemy.orm import Session

from herald.db.models import JobState, PodcastJob

logger = logging.getLogger("herald.telegram.resolver")

# Hex/UUID character validation pattern
SAFE_ID_PATTERN = re.compile(r"^[0-9a-fA-F\-]{4,36}$")


def resolve_user_job(
    db: Session,
    telegram_user_id: int | str,
    telegram_chat_id: int | str,
    identifier: str | None = None,
    completed_only: bool = False,
) -> PodcastJob | None:
    """
    Safely resolve a PodcastJob belonging to the specific Telegram user and chat context.
    - If completed_only is True, filters only jobs in JobState.COMPLETE.
    - If identifier is omitted/empty, returns the user's latest matching job.
    - If identifier is provided:
        - Validates against injection/wildcard characters.
        - Exact full UUID match if 36 chars.
        - Unambiguous prefix match (min 4 chars) only if exactly ONE caller job matches.
        - Ambiguous prefix (>1 matches) returns None.
    - Strictly enforces tenant isolation: telegram_user_id and telegram_chat_id must match.
    """
    try:
        uid_int = int(telegram_user_id)
        cid_int = int(telegram_chat_id)
    except (ValueError, TypeError):
        return None

    query = db.query(PodcastJob).filter(
        PodcastJob.transport == "telegram",
        PodcastJob.telegram_user_id == uid_int,
        PodcastJob.telegram_chat_id == cid_int,
    )

    if completed_only:
        query = query.filter(PodcastJob.status == JobState.COMPLETE.value)

    clean_id = (identifier or "").strip()

    if not clean_id:
        # Default: latest matching job for this caller
        return query.order_by(
            PodcastJob.completed_at.desc(),
            PodcastJob.created_at.desc(),
        ).first()

    # Reject wildcard characters and invalid hex/UUID strings
    if any(c in clean_id for c in ("%", "_", "*", "?", " ", "\n", "\r", "'", '"', ";")):
        return None

    if not SAFE_ID_PATTERN.match(clean_id):
        return None

    # 1. Exact full UUID match
    if len(clean_id) == 36:
        return query.filter(PodcastJob.id == clean_id).first()

    # 2. Prefix match (minimum 4 characters)
    if len(clean_id) >= 4:
        matches = (
            query.filter(PodcastJob.id.startswith(clean_id))
            .order_by(PodcastJob.created_at.desc())
            .limit(2)
            .all()
        )
        if len(matches) == 1:
            return matches[0]
        # Ambiguous match (len == 2) or no match (len == 0) -> return None
        return None

    return None
