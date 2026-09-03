"""
Diagnostics Support Package Exporter for Herald.
Generates a sanitized, structured ZIP bundle containing job execution telemetry,
timings, AI interaction evidence, errors, and safe environment configuration.
"""

import json
import logging
import re
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from herald.config import settings
from herald.db.models import (
    AIInteraction,
    JobProcessingMetric,
    JobStateTransition,
    PodcastJob,
    PodcastTTSChunk,
)
from herald.services.redaction import (
    build_safe_environment_summary,
    redact_dict,
    redact_text,
)

logger = logging.getLogger("herald.diagnostics.export")


def _sanitize_slug(text: str) -> str:
    """Produce a safe ASCII-only slug for filenames."""
    if not text:
        return "job"
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", text)
    return clean[:32].strip("_") or "job"


def _to_utc(dt: datetime | None) -> datetime | None:
    """Normalize datetime to UTC aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def build_job_summary_dict(job: PodcastJob, db: Session) -> dict:
    """Build a high-level truthful summary dictionary for a job."""
    script_obj = job.script_json or {}
    title = script_obj.get("episode_title") or job.custom_title or "Herald Episode"

    # Compute duration
    total_sec = None
    active_sec = None
    created_utc = _to_utc(job.created_at)
    if created_utc:
        end_time = _to_utc(job.completed_at) or _to_utc(job.delivered_at) or datetime.now(UTC)
        total_sec = max(0, int((end_time - created_utc).total_seconds()))
        app_req_utc = _to_utc(job.approval_requested_at)
        app_done_utc = _to_utc(job.approved_at)
        if app_req_utc and app_done_utc:
            hold_sec = (app_done_utc - app_req_utc).total_seconds()
            active_sec = max(0, int(total_sec - hold_sec))
        else:
            active_sec = total_sec

    # TTS chunks count
    tts_chunks_count = (
        db.query(PodcastTTSChunk).filter(PodcastTTSChunk.job_id == job.id).count()
    )

    # AI interaction metrics
    interactions = (
        db.query(AIInteraction)
        .filter(AIInteraction.job_id == job.id)
        .order_by(AIInteraction.created_at.asc())
        .all()
    )
    ai_call_count = len(interactions)
    total_tokens = sum(i.total_tokens for i in interactions if i.total_tokens is not None) if ai_call_count > 0 else None

    # AI identity
    ai_prov = "None (Literal)" if job.request_mode == "literal" else (interactions[0].provider if interactions else (settings.AI_PROVIDER or "None"))
    ai_model = "None" if job.request_mode == "literal" else (interactions[0].model if interactions else (job.gemini_model or settings.GEMINI_MODEL))

    return {
        "job_id": job.id,
        "title": redact_text(title),
        "status": job.status,
        "transport": job.transport,
        "request_mode": job.request_mode,
        "research_depth": job.research_depth,
        "source_type": job.source_type,
        "ai_provider": ai_prov,
        "ai_model": ai_model,
        "voice": job.kokoro_voice or job.custom_voice or settings.KOKORO_VOICE,
        "speed": job.kokoro_speed or job.custom_speed or settings.KOKORO_SPEED,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.claimed_at.isoformat() if job.claimed_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "delivered_at": job.delivered_at.isoformat() if job.delivered_at else None,
        "total_duration_seconds": total_sec,
        "active_processing_seconds": active_sec,
        "tts_chunk_count": tts_chunks_count,
        "audio_duration_seconds": job.audio_duration_seconds,
        "audio_bytes": job.audio_bytes,
        "ai_interaction_count": ai_call_count,
        "ai_total_tokens": total_tokens,
        "attempt_count": job.attempt_count,
        "synthesis_attempt_count": job.synthesis_attempt_count,
        "delivery_attempt_count": job.delivery_attempt_count,
        "failed_stage": job.failed_stage,
        "error_code": job.error_code,
        "error_detail": redact_text(job.error_detail),
    }


def build_job_dict(job: PodcastJob) -> dict:
    """Build a sanitized representation of the job database model, omitting raw sources/prompts."""
    raw = {
        "id": job.id,
        "transport": job.transport,
        "status": job.status,
        "request_mode": job.request_mode,
        "research_depth": job.research_depth,
        "source_type": job.source_type,
        "source_url": job.source_url,
        "source_hash": job.source_hash,
        "custom_voice": job.custom_voice,
        "custom_speed": job.custom_speed,
        "custom_title": job.custom_title,
        "tts_chunk_chars": job.tts_chunk_chars,
        "verify_final_script": job.verify_final_script,
        "attempt_count": job.attempt_count,
        "synthesis_attempt_count": job.synthesis_attempt_count,
        "delivery_attempt_count": job.delivery_attempt_count,
        "failed_stage": job.failed_stage,
        "verify_repair_count": job.verify_repair_count,
        "research_repair_count": job.research_repair_count,
        "research_search_count": job.research_search_count,
        "research_source_count": job.research_source_count,
        "completed_chunk_index": job.completed_chunk_index,
        "audio_bytes": job.audio_bytes,
        "audio_duration_seconds": job.audio_duration_seconds,
        "kokoro_voice": job.kokoro_voice,
        "kokoro_speed": job.kokoro_speed,
        "gemini_model": job.gemini_model,
        "approval_required": job.approval_required,
        "approval_requested_at": job.approval_requested_at.isoformat() if job.approval_requested_at else None,
        "approved_at": job.approved_at.isoformat() if job.approved_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "claimed_at": job.claimed_at.isoformat() if job.claimed_at else None,
        "audio_ready_at": job.audio_ready_at.isoformat() if job.audio_ready_at else None,
        "delivered_at": job.delivered_at.isoformat() if job.delivered_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error_code": job.error_code,
        "error_detail": job.error_detail,
    }
    return redact_dict(raw)


def build_timings_dict(job: PodcastJob, db: Session) -> dict:
    """Extract and format processing metrics and state transitions."""
    metrics = (
        db.query(JobProcessingMetric)
        .filter(JobProcessingMetric.job_id == job.id)
        .order_by(JobProcessingMetric.started_at.asc())
        .all()
    )
    metric_list = []
    for m in metrics:
        metric_list.append(
            {
                "stage": m.stage,
                "substage": m.substage,
                "attempt": m.attempt,
                "sequence_index": m.sequence_index,
                "status": m.status,
                "started_at": m.started_at.isoformat() if m.started_at else None,
                "finished_at": m.finished_at.isoformat() if m.finished_at else None,
                "duration_ms": m.duration_ms,
                "input_chars": m.input_chars,
                "output_bytes": m.output_bytes,
                "audio_duration_ms": m.audio_duration_ms,
                "metadata": redact_dict(m.metadata_json),
            }
        )

    transitions = (
        db.query(JobStateTransition)
        .filter(JobStateTransition.job_id == job.id)
        .order_by(JobStateTransition.created_at.asc())
        .all()
    )
    transition_list = []
    for t in transitions:
        transition_list.append(
            {
                "from_state": t.from_state,
                "to_state": t.to_state,
                "component": t.component,
                "message": redact_text(t.message),
                "error_category": t.error_category,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
        )

    return {
        "job_id": job.id,
        "stage_metrics": metric_list,
        "state_transitions": transition_list,
    }


def build_ai_interactions_dict(job: PodcastJob, db: Session) -> dict:
    """Extract all AI interactions recorded for the job."""
    records = (
        db.query(AIInteraction)
        .filter(AIInteraction.job_id == job.id)
        .order_by(AIInteraction.started_at.asc())
        .all()
    )
    interactions = []
    for r in records:
        interactions.append(
            {
                "id": r.id,
                "provider": r.provider,
                "model": r.model,
                "operation": r.operation,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "duration_ms": r.duration_ms,
                "success": r.success,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "error_category": r.error_category,
                "error_message": redact_text(r.error_message),
                "metadata": redact_dict(r.metadata_json),
            }
        )

    return {
        "job_id": job.id,
        "interaction_count": len(interactions),
        "interactions": interactions,
    }


def build_errors_dict(job: PodcastJob, db: Session) -> dict:
    """Compile all errors across transitions, metrics, AI interactions, and job errors."""
    errors = []

    if job.error_code or job.error_detail:
        errors.append(
            {
                "source": "podcast_jobs",
                "stage": job.failed_stage,
                "error_code": job.error_code,
                "error_detail": redact_text(job.error_detail),
            }
        )

    transitions = (
        db.query(JobStateTransition)
        .filter(
            JobStateTransition.job_id == job.id,
            JobStateTransition.error_category.isnot(None),
        )
        .order_by(JobStateTransition.created_at.asc())
        .all()
    )
    for t in transitions:
        errors.append(
            {
                "source": "state_transition",
                "from_state": t.from_state,
                "to_state": t.to_state,
                "component": t.component,
                "error_category": t.error_category,
                "message": redact_text(t.message),
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
        )

    failed_metrics = (
        db.query(JobProcessingMetric)
        .filter(
            JobProcessingMetric.job_id == job.id,
            JobProcessingMetric.status == "failed",
        )
        .all()
    )
    for m in failed_metrics:
        errors.append(
            {
                "source": "processing_metric",
                "stage": m.stage,
                "substage": m.substage,
                "attempt": m.attempt,
                "metadata": redact_dict(m.metadata_json),
                "started_at": m.started_at.isoformat() if m.started_at else None,
            }
        )

    failed_ai = (
        db.query(AIInteraction)
        .filter(
            AIInteraction.job_id == job.id,
            AIInteraction.success.is_(False),
        )
        .all()
    )
    for a in failed_ai:
        errors.append(
            {
                "source": "ai_interaction",
                "provider": a.provider,
                "model": a.model,
                "operation": a.operation,
                "error_category": a.error_category,
                "error_message": redact_text(a.error_message),
                "started_at": a.started_at.isoformat() if a.started_at else None,
            }
        )

    return {
        "job_id": job.id,
        "error_count": len(errors),
        "errors": errors,
    }


def generate_job_diagnostics_zip(db: Session, job: PodcastJob) -> Path:
    """
    Generate a complete support diagnostics ZIP file for the specified job.
    Creates a dedicated work folder, writes 6 JSON artifacts, compresses into a ZIP,
    cleans the temporary staging directory, and returns the absolute path to the generated ZIP.
    """
    work_base = Path(settings.HERALD_WORK_DIR) / "diagnostics"
    work_base.mkdir(parents=True, exist_ok=True)

    ts_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    slug = _sanitize_slug(job.custom_title or job.id[:8])
    staging_dir = work_base / f"staging_{job.id[:8]}_{ts_str}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    zip_filename = f"herald_diagnostics_{slug}_{job.id[:8]}_{ts_str}.zip"
    zip_path = work_base / zip_filename

    try:
        # 1. summary.json
        summary_data = build_job_summary_dict(job, db)
        (staging_dir / "summary.json").write_text(json.dumps(summary_data, indent=2), encoding="utf-8")

        # 2. job.json
        job_data = build_job_dict(job)
        (staging_dir / "job.json").write_text(json.dumps(job_data, indent=2), encoding="utf-8")

        # 3. timings.json
        timings_data = build_timings_dict(job, db)
        (staging_dir / "timings.json").write_text(json.dumps(timings_data, indent=2), encoding="utf-8")

        # 4. ai_interactions.json
        ai_data = build_ai_interactions_dict(job, db)
        (staging_dir / "ai_interactions.json").write_text(json.dumps(ai_data, indent=2), encoding="utf-8")

        # 5. errors.json
        errors_data = build_errors_dict(job, db)
        (staging_dir / "errors.json").write_text(json.dumps(errors_data, indent=2), encoding="utf-8")

        # 6. environment-summary.json
        env_data = build_safe_environment_summary()
        (staging_dir / "environment-summary.json").write_text(json.dumps(env_data, indent=2), encoding="utf-8")

        # Create ZIP
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for file_path in staging_dir.iterdir():
                if file_path.is_file():
                    zip_file.write(file_path, arcname=file_path.name)

        return zip_path
    finally:
        # Clean up staging directory
        try:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Failed to remove staging directory {staging_dir}: {e}")
