"""
Provider-Neutral AI Interaction Recording Service for Herald.
Persists evidence of external AI calls (duration, tokens, success/failure, sanitized errors, request/response evidence)
into the `ai_interactions` table for support diagnostics.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from herald.db.connection import SessionLocal
from herald.db.models import AIInteraction
from herald.services.redaction import redact_dict, sanitize_error

logger = logging.getLogger("herald.ai.recorder")


def record_ai_interaction(
    job_id: str | None,
    provider: str,
    model: str,
    operation: str,
    started_at: datetime,
    completed_at: datetime | None = None,
    attempt: int = 1,
    http_status: int | None = None,
    provider_request_id: str | None = None,
    input_chars: int | None = None,
    success: bool = True,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    error: Exception | str | None = None,
    error_category: str | None = None,
    error_message: str | None = None,
    request_json: dict[str, Any] | None = None,
    response_json: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    db: Any = None,
) -> str | None:
    """
    Safely record an AI interaction into the database in an isolated transaction.
    Non-fatal: any persistence error is caught and logged, never crashing the podcast pipeline.
    """
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)

    if completed_at is None:
        completed_at = datetime.now(UTC)
    elif completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=UTC)

    duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))

    if error:
        cat, msg = sanitize_error(error)
        error_category = error_category or cat
        error_message = error_message or msg

    clean_meta = redact_dict(metadata) if metadata else None
    clean_req = redact_dict(request_json) if request_json else None
    clean_resp = redact_dict(response_json) if response_json else None

    # Derive total tokens if prompt and completion are provided
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens

    record_id = str(uuid.uuid4())
    interaction = AIInteraction(
        id=record_id,
        job_id=job_id,
        provider=str(provider or "unknown").lower(),
        model=str(model or "unknown"),
        operation=str(operation or "unknown"),
        attempt=attempt,
        http_status=http_status,
        provider_request_id=str(provider_request_id) if provider_request_id else None,
        input_chars=input_chars,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        success=bool(success),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        error_category=error_category,
        error_message=error_message,
        request_json_sanitized=clean_req,
        response_json_sanitized=clean_resp,
        metadata_json=clean_meta,
        created_at=datetime.now(UTC),
    )

    should_close = False
    session = db
    if session is None:
        session = SessionLocal()
        should_close = True

    try:
        session.add(interaction)
        session.commit()
        return record_id
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(f"Non-fatal warning: failed to persist AI interaction for job '{job_id}': {e}")
        return None
    finally:
        if should_close:
            try:
                session.close()
            except Exception:
                pass
