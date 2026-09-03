"""
Centralized Non-Fatal Job Diagnostic Event Recording Service.
Records high-level pipeline, worker, audio, and delivery events for support diagnostics.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from herald.db.connection import SessionLocal
from herald.db.models import JobDiagnosticEvent
from herald.services.redaction import redact_dict, redact_text

logger = logging.getLogger("herald.services.diagnostic_recorder")


def record_job_diagnostic_event(
    job_id: str | None,
    level: str,
    component: str,
    event_type: str,
    message: str,
    metadata: dict[str, Any] | None = None,
    db: Session | None = None,
    timestamp: datetime | None = None,
) -> JobDiagnosticEvent | None:
    """
    Persist a structured job diagnostic event.
    Non-fatal: catches and logs any persistence exception without failing the pipeline.
    """
    if not job_id:
        return None

    event_time = timestamp or datetime.now(UTC)
    sanitized_message = redact_text(message or "")
    sanitized_metadata = redact_dict(metadata) if metadata else None
    lvl_clean = (level or "INFO").upper()

    event = JobDiagnosticEvent(
        id=str(uuid.uuid4()),
        job_id=job_id,
        timestamp=event_time,
        level=lvl_clean,
        component=component or "general",
        event_type=event_type or "EVENT",
        message=sanitized_message,
        metadata_json_sanitized=sanitized_metadata,
        created_at=datetime.now(UTC),
    )

    should_close = False
    session = db
    if session is None:
        session = SessionLocal()
        should_close = True

    try:
        session.add(event)
        session.commit()
        return event
    except Exception as e:
        session.rollback()
        logger.warning("Failed to record job diagnostic event for job '%s': %s", job_id, e)
        return None
    finally:
        if should_close:
            session.close()
