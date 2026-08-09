import logging
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Dict, Optional

from herald.config import settings
from herald.db.connection import SessionLocal
from herald.db.models import JobProcessingMetric

logger = logging.getLogger("herald.performance_metrics")

# Sensitive key patterns that must be stripped from metadata_json
FORBIDDEN_METADATA_KEYS = {
    "source_text",
    "narration",
    "email_body",
    "full_email",
    "api_key",
    "token",
    "secret",
    "password",
    "credential",
    "auth",
}


def sanitize_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Recursively sanitize metadata dictionary to ensure no sensitive text or credentials leak into DB metrics."""
    if not metadata or not isinstance(metadata, dict):
        return None

    cleaned: Dict[str, Any] = {}
    for k, v in metadata.items():
        if any(bad in str(k).lower() for bad in FORBIDDEN_METADATA_KEYS):
            cleaned[k] = "[REDACTED]"
        elif isinstance(v, dict):
            cleaned[k] = sanitize_metadata(v)
        elif isinstance(v, (str, int, float, bool, type(None))):
            cleaned[k] = v
        else:
            cleaned[k] = str(v)
    return cleaned


def record_stage_metric(
    job_id: str,
    stage: str,
    started_at: datetime,
    finished_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    status: str = "success",
    substage: Optional[str] = None,
    attempt: Optional[int] = None,
    sequence_index: Optional[int] = None,
    input_chars: Optional[int] = None,
    output_bytes: Optional[int] = None,
    audio_duration_ms: Optional[int] = None,
    metadata_json: Optional[Dict[str, Any]] = None,
    is_attempt_metric: bool = False,
) -> Optional[str]:
    """
    Safely record or update a job processing stage metric in an isolated DB transaction.
    Non-fatal: any failure is caught, logged as a warning, and returns None.

    If is_attempt_metric is False, performs deterministic upsert on (job_id, stage) to maintain idempotency.
    If is_attempt_metric is True, inserts a new row per attempt (e.g. KOKORO_REQUEST).
    """
    if not settings.HERALD_METRICS_ENABLED:
        return None

    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if finished_at and finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=UTC)

    if finished_at and duration_ms is None and started_at:
        duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))

    clean_meta = sanitize_metadata(metadata_json)
    db = None
    try:
        db = SessionLocal()

        metric = None
        if not is_attempt_metric:
            # Deterministic upsert on (job_id, stage)
            metric = (
                db.query(JobProcessingMetric)
                .filter(JobProcessingMetric.job_id == job_id, JobProcessingMetric.stage == stage)
                .first()
            )

        if metric is None:
            metric_id = str(uuid.uuid4())
            metric = JobProcessingMetric(
                id=metric_id,
                job_id=job_id,
                stage=stage,
                substage=substage,
                attempt=attempt,
                sequence_index=sequence_index,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                status=status,
                input_chars=input_chars,
                output_bytes=output_bytes,
                audio_duration_ms=audio_duration_ms,
                metadata_json=clean_meta,
                created_at=datetime.now(UTC),
            )
            db.add(metric)
        else:
            metric.substage = substage or metric.substage
            metric.attempt = attempt if attempt is not None else metric.attempt
            metric.sequence_index = sequence_index if sequence_index is not None else metric.sequence_index
            metric.started_at = started_at
            metric.finished_at = finished_at or metric.finished_at
            metric.duration_ms = duration_ms if duration_ms is not None else metric.duration_ms
            metric.status = status
            metric.input_chars = input_chars if input_chars is not None else metric.input_chars
            metric.output_bytes = output_bytes if output_bytes is not None else metric.output_bytes
            metric.audio_duration_ms = audio_duration_ms if audio_duration_ms is not None else metric.audio_duration_ms
            if clean_meta:
                existing_meta = metric.metadata_json or {}
                existing_meta.update(clean_meta)
                metric.metadata_json = existing_meta

        db.commit()
        return metric.id
    except Exception as e:
        if db:
            try:
                db.rollback()
            except Exception:
                pass
        logger.warning(f"Non-fatal warning: failed to record metric for job '{job_id}' stage '{stage}': {e}")
        return None
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@contextmanager
def metric_timer(
    job_id: str,
    stage: str,
    substage: Optional[str] = None,
    attempt: Optional[int] = None,
    sequence_index: Optional[int] = None,
    input_chars: Optional[int] = None,
    metadata_json: Optional[Dict[str, Any]] = None,
    is_attempt_metric: bool = False,
):
    """
    Context manager for measuring stage wall-clock duration with time.monotonic() and recording metric safely on exit.
    """
    started_utc = datetime.now(UTC)
    t0 = time.monotonic()
    context: Dict[str, Any] = {
        "status": "success",
        "output_bytes": None,
        "audio_duration_ms": None,
        "metadata": metadata_json or {},
    }
    try:
        yield context
    except Exception as exc:
        context["status"] = "failed"
        meta = context.get("metadata") or {}
        meta["error_type"] = exc.__class__.__name__
        context["metadata"] = meta
        raise
    finally:
        elapsed_sec = time.monotonic() - t0
        duration_ms = max(0, int(elapsed_sec * 1000))
        finished_utc = datetime.now(UTC)

        record_stage_metric(
            job_id=job_id,
            stage=stage,
            substage=substage,
            attempt=attempt,
            sequence_index=sequence_index,
            started_at=started_utc,
            finished_at=finished_utc,
            duration_ms=duration_ms,
            status=context.get("status", "success"),
            input_chars=input_chars,
            output_bytes=context.get("output_bytes"),
            audio_duration_ms=context.get("audio_duration_ms"),
            metadata_json=context.get("metadata"),
            is_attempt_metric=is_attempt_metric,
        )
