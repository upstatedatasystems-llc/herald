"""
Diagnostics Support Package Exporter for Herald.
Generates a sanitized, structured ZIP bundle containing the complete job execution telemetry,
episode details markdown, source text, script artifacts, TTS chunks, timings, AI interaction evidence,
diagnostic events, and safe environment configuration.
"""

import json
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from herald.audio.artifact_generator import ensure_details_artifact
from herald.config import settings
from herald.db.connection import SessionLocal
from herald.db.models import (
    AIInteraction,
    JobDiagnosticEvent,
    JobProcessingMetric,
    JobState,
    JobStateTransition,
    PodcastJob,
    PodcastTTSChunk,
)
from herald.services.redaction import (
    build_safe_environment_summary,
    redact_dict,
    redact_text,
    redact_value,
    sanitize_content_dict,
    scan_for_secrets,
)

logger = logging.getLogger("herald.diagnostics.export")

DIAGNOSTIC_SCHEMA_VERSION = "2.0.0"
MAX_DIAGNOSTIC_BYTES = getattr(settings, "DIAGNOSTICS_MAX_BYTES", 8 * 1024 * 1024)
TARGET_DIAGNOSTIC_BYTES = 5 * 1024 * 1024


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


def render_script_markdown(script_data: dict[str, Any] | None) -> str:
    """Render structured script dictionary into human-readable Markdown."""
    if not script_data or not isinstance(script_data, dict):
        return "# Podcast Script\n\nNo script data available."

    title = script_data.get("episode_title") or "Herald Episode"
    desc = script_data.get("episode_description") or ""
    est_mins = script_data.get("estimated_minutes")
    src_title = script_data.get("source_title") or ""
    warnings = script_data.get("warnings") or []
    segments = script_data.get("segments") or []

    lines = [
        f"# {title}",
        "",
        f"**Source**: {src_title}" if src_title else "",
        f"**Estimated Duration**: {est_mins} minutes" if est_mins else "",
        f"**Description**: {desc}" if desc else "",
        "",
    ]
    if warnings:
        lines.append("## Warnings")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

    lines.append("## Segments")
    lines.append("")
    for s in segments:
        heading = s.get("heading") or f"Segment {s.get('order', 1)}"
        narration = s.get("narration") or ""
        lines.append(f"### {heading}")
        lines.append("")
        lines.append(narration)
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def build_manifest_dict(
    job: PodcastJob,
    db: Session,
    included_files: list[str],
    truncated_files: list[str],
    truncations_detail: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build canonical manifest.json describing the diagnostics bundle."""
    from herald.telegram.formatters import get_job_ai_identity

    prov_name, model_name = get_job_ai_identity(job)

    created_utc = _to_utc(job.created_at)
    completed_utc = _to_utc(job.completed_at) or _to_utc(job.delivered_at)
    app_req_utc = _to_utc(job.approval_requested_at)
    app_done_utc = _to_utc(job.approved_at)

    total_seconds = None
    if created_utc and completed_utc:
        raw_sec = max(0, int((completed_utc - created_utc).total_seconds()))
        if app_req_utc and app_done_utc:
            hold_sec = max(0, int((app_done_utc - app_req_utc).total_seconds()))
            total_seconds = max(1, raw_sec - hold_sec)
        else:
            total_seconds = raw_sec

    source_words = len((job.source_text or "").split())
    script_obj = job.script_json or {}
    narration_words = 0
    for seg in script_obj.get("segments", []):
        narration_words += len((seg.get("narration") or "").split())

    # Authoritative chunk counts from podcast_tts_chunks table
    actual_chunks_count = (
        db.query(PodcastTTSChunk).filter(PodcastTTSChunk.job_id == job.id).count()
    )
    completed_chunks_count = (
        db.query(PodcastTTSChunk)
        .filter(PodcastTTSChunk.job_id == job.id, PodcastTTSChunk.status == "COMPLETED")
        .count()
    )

    conc_profile = "default"
    try:
        from herald.concurrency import resolve_concurrency_settings
        conc = resolve_concurrency_settings(profile=settings.HERALD_CONCURRENCY_PROFILE)
        conc_profile = conc.profile
    except Exception:
        pass

    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "herald_version": getattr(settings, "HERALD_VERSION", "2.0.0"),
        "job_id": job.id,
        "bundle_generated_at": datetime.now(UTC).isoformat(),
        "job_created_at": created_utc.isoformat() if created_utc else None,
        "job_completed_at": completed_utc.isoformat() if completed_utc else None,
        "request_mode": job.request_mode,
        "research_depth": job.research_depth,
        "source_type": job.source_type,
        "ai_provider": prov_name or "None (Literal)",
        "ai_model": model_name or "local-literal",
        "research_model": getattr(job, "research_model", None),
        "voice": job.kokoro_voice or job.custom_voice or settings.KOKORO_VOICE,
        "speed": job.kokoro_speed or job.custom_speed or settings.KOKORO_SPEED,
        "source_word_count": source_words,
        "narration_word_count": narration_words,
        "predicted_duration_minutes": script_obj.get("estimated_minutes"),
        "actual_duration_seconds": job.audio_duration_seconds,
        "actual_tts_chunk_count": actual_chunks_count,
        "completed_tts_chunk_count": completed_chunks_count,
        "total_processing_seconds": total_seconds,
        "status": job.status,
        "error_code": job.error_code,
        "concurrency_profile": conc_profile,
        "included_files": sorted(included_files),
        "truncated_files": sorted(truncated_files),
        "truncations": truncations_detail or [],
    }


def get_diagnostics_base_dir() -> Path:
    """Return the canonical directory for diagnostics archives: logs/diagnostics."""
    base = Path(getattr(settings, "HERALD_LOG_DIR", "logs")) / "diagnostics"
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_terminal_diagnostics_path(job_id: str, terminal_status: str) -> Path:
    """Return canonical path for a terminal job's support bundle: logs/diagnostics/<job_id>_<status>.zip"""
    clean_status = (terminal_status or "COMPLETE").upper()
    return get_diagnostics_base_dir() / f"{job_id}_{clean_status}.zip"


def generate_job_diagnostics_zip(
    db: Session,
    job: PodcastJob,
    target_zip_path: Path | None = None,
) -> Path:
    """
    Generate a complete support diagnostics ZIP file for the specified job.
    Creates a dedicated work folder, writes all canonical artifacts, enforces size limits,
    performs a pre-send fail-closed secret scan, cleans temporary staging, and returns ZIP path.
    Uses unique PID+UUID temporary staging for safe concurrent generation.
    """
    work_base = get_diagnostics_base_dir()

    ts_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    slug = _sanitize_slug(job.custom_title or job.id[:8])
    staging_dir = work_base / f"staging_{job.id[:8]}_{ts_str}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    if target_zip_path is None:
        target_zip_path = work_base / f"herald-diagnostics-{slug}-{job.id[:8]}-{ts_str}.zip"

    # Unique PID+UUID temp file for safe concurrent generation
    tmp_zip_path = work_base / f"{target_zip_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"

    included_files: list[str] = []
    truncated_files: list[str] = []

    try:
        # 1. README.txt
        readme_content = f"""Herald Diagnostics Package
==========================
Job ID: {job.id}
Mode: {job.request_mode}
Generated: {datetime.now(UTC).isoformat()}
Herald Version: {getattr(settings, 'HERALD_VERSION', '2.0.0')}

This archive contains sanitized diagnostic and execution telemetry for support analysis.
Configured API keys, credentials, and Authorization headers have been scrubbed.
"""
        (staging_dir / "README.txt").write_text(readme_content, encoding="utf-8")
        included_files.append("README.txt")

        # 2. episode-details.md (generated via unified details generator)
        try:
            details_out = ensure_details_artifact(job, target_dir=staging_dir, db=db)
            if details_out.exists():
                renamed_details = staging_dir / "episode-details.md"
                if details_out != renamed_details:
                    shutil.move(str(details_out), str(renamed_details))
                included_files.append("episode-details.md")
        except Exception as e:
            logger.warning(f"Could not render episode-details.md: {e}")
            (staging_dir / "episode-details.md").write_text(f"# Episode Details\n\nUnavailable: {e}", encoding="utf-8")
            included_files.append("episode-details.md")

        # 3. source.txt (full source text with actual secrets scrubbed)
        raw_source = job.source_text or ""
        sanitized_source = redact_text(raw_source)
        (staging_dir / "source.txt").write_text(sanitized_source, encoding="utf-8")
        included_files.append("source.txt")

        # 4. script.json & script.md (preserve narration / segments)
        script_dict = job.script_json or {}
        sanitized_script_dict = sanitize_content_dict(script_dict)
        (staging_dir / "script.json").write_text(json.dumps(sanitized_script_dict, indent=2), encoding="utf-8")
        included_files.append("script.json")

        script_md = render_script_markdown(sanitized_script_dict)
        (staging_dir / "script.md").write_text(script_md, encoding="utf-8")
        included_files.append("script.md")

        # 5. state-transitions.json
        transitions = (
            db.query(JobStateTransition)
            .filter(JobStateTransition.job_id == job.id)
            .order_by(JobStateTransition.created_at.asc())
            .all()
        )
        transition_list = [
            {
                "from_state": t.from_state,
                "to_state": t.to_state,
                "component": t.component,
                "message": redact_text(t.message),
                "error_category": t.error_category,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in transitions
        ]
        (staging_dir / "state-transitions.json").write_text(json.dumps(transition_list, indent=2), encoding="utf-8")
        included_files.append("state-transitions.json")

        # 6. processing-metrics.json
        metrics = (
            db.query(JobProcessingMetric)
            .filter(JobProcessingMetric.job_id == job.id)
            .order_by(JobProcessingMetric.started_at.asc())
            .all()
        )
        metric_list = [
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
            for m in metrics
        ]
        (staging_dir / "processing-metrics.json").write_text(json.dumps(metric_list, indent=2), encoding="utf-8")
        included_files.append("processing-metrics.json")

        # 7. tts-chunks.json
        chunks = (
            db.query(PodcastTTSChunk)
            .filter(PodcastTTSChunk.job_id == job.id)
            .order_by(PodcastTTSChunk.chunk_index.asc())
            .all()
        )
        chunk_list = [
            {
                "chunk_index": c.chunk_index,
                "status": c.status,
                "text_hash": c.text_hash,
                "audio_duration": c.audio_duration,
                "attempt_count": c.attempt_count,
                "error_detail": redact_text(c.error_detail),
                "started_at": c.started_at.isoformat() if c.started_at else None,
                "completed_at": c.completed_at.isoformat() if c.completed_at else None,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in chunks
        ]
        (staging_dir / "tts-chunks.json").write_text(json.dumps(chunk_list, indent=2), encoding="utf-8")
        included_files.append("tts-chunks.json")

        # 8. diagnostic-events.jsonl
        events = (
            db.query(JobDiagnosticEvent)
            .filter(JobDiagnosticEvent.job_id == job.id)
            .order_by(JobDiagnosticEvent.timestamp.asc())
            .all()
        )
        orig_events_count = len(events)
        truncations_detail: list[dict[str, Any]] = []

        # Size bounding on events: keep last 1000 events if huge
        if len(events) > 1000:
            events = events[-1000:]
            truncated_files.append("diagnostic-events.jsonl")
            truncations_detail.append({
                "file": "diagnostic-events.jsonl",
                "reason": "event_count_limit",
                "original_count": orig_events_count,
                "retained_count": len(events),
            })

        event_lines = [
            json.dumps(
                {
                    "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                    "level": e.level,
                    "component": e.component,
                    "event_type": e.event_type,
                    "message": redact_text(e.message),
                    "metadata": redact_dict(e.metadata_json_sanitized),
                }
            )
            for e in events
        ]
        (staging_dir / "diagnostic-events.jsonl").write_text("\n".join(event_lines) + ("\n" if event_lines else ""), encoding="utf-8")
        included_files.append("diagnostic-events.jsonl")

        # 9. ai-interactions.json
        interactions = (
            db.query(AIInteraction)
            .filter(AIInteraction.job_id == job.id)
            .order_by(AIInteraction.started_at.asc())
            .all()
        )
        orig_ai_count = len(interactions)
        if len(interactions) > 200:
            interactions = interactions[-200:]
            truncated_files.append("ai-interactions.json")
            truncations_detail.append({
                "file": "ai-interactions.json",
                "reason": "interaction_count_limit",
                "original_count": orig_ai_count,
                "retained_count": len(interactions),
            })

        ai_list = [
            {
                "id": r.id,
                "provider": r.provider,
                "model": r.model,
                "operation": r.operation,
                "attempt": r.attempt,
                "http_status": r.http_status,
                "provider_request_id": r.provider_request_id,
                "input_chars": r.input_chars,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "duration_ms": r.duration_ms,
                "success": r.success,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "error_category": r.error_category,
                "error_message": redact_text(r.error_message),
                "request_evidence": redact_dict(r.request_json_sanitized),
                "response_evidence": redact_dict(r.response_json_sanitized),
                "metadata": redact_dict(r.metadata_json),
            }
            for r in interactions
        ]
        (staging_dir / "ai-interactions.json").write_text(json.dumps(ai_list, indent=2), encoding="utf-8")
        included_files.append("ai-interactions.json")

        # 10. config-sanitized.json
        env_summary = build_safe_environment_summary()
        (staging_dir / "config-sanitized.json").write_text(json.dumps(env_summary, indent=2), encoding="utf-8")
        included_files.append("config-sanitized.json")

        # 11. errors.json (if errors exist)
        if job.error_code or job.error_detail:
            err_dict = {
                "error_code": job.error_code,
                "error_detail": redact_text(job.error_detail),
                "failed_stage": job.failed_stage,
                "attempt_count": job.attempt_count,
            }
            (staging_dir / "errors.json").write_text(json.dumps(err_dict, indent=2), encoding="utf-8")
            included_files.append("errors.json")

        # 11b. failure-diagnostics.json (if auto_diagnostics_json exists)
        if job.auto_diagnostics_json:
            (staging_dir / "failure-diagnostics.json").write_text(
                json.dumps(redact_value(job.auto_diagnostics_json), indent=2), encoding="utf-8"
            )
            included_files.append("failure-diagnostics.json")

        # 12. Research files (conditional on research data)
        if job.research_grounding_json or job.research_json or job.research_audit_json:
            research_dir = staging_dir / "research"
            research_dir.mkdir(exist_ok=True)
            if job.research_grounding_json:
                (research_dir / "grounding.json").write_text(
                    json.dumps(sanitize_content_dict(job.research_grounding_json), indent=2), encoding="utf-8"
                )
                included_files.append("research/grounding.json")
            if job.research_json:
                (research_dir / "dossier.json").write_text(
                    json.dumps(sanitize_content_dict(job.research_json), indent=2), encoding="utf-8"
                )
                included_files.append("research/dossier.json")
            if job.research_audit_json:
                (research_dir / "audit.json").write_text(
                    json.dumps(sanitize_content_dict(job.research_audit_json), indent=2), encoding="utf-8"
                )
                included_files.append("research/audit.json")

        # Multi-stage progressive size reduction when exceeding budget
        staged_bytes = sum(f.stat().st_size for f in staging_dir.rglob("*") if f.is_file())
        if staged_bytes > TARGET_DIAGNOSTIC_BYTES:
            # Stage 1: Reduce events to last 200
            if len(events) > 200:
                events = events[-200:]
                event_lines = [
                    json.dumps(
                        {
                            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                            "level": e.level,
                            "component": e.component,
                            "event_type": e.event_type,
                            "message": redact_text(e.message),
                            "metadata": redact_dict(e.metadata_json_sanitized),
                        }
                    )
                    for e in events
                ]
                (staging_dir / "diagnostic-events.jsonl").write_text("\n".join(event_lines) + ("\n" if event_lines else ""), encoding="utf-8")
                if "diagnostic-events.jsonl" not in truncated_files:
                    truncated_files.append("diagnostic-events.jsonl")
                truncations_detail.append({
                    "file": "diagnostic-events.jsonl",
                    "stage": 1,
                    "reason": "target_budget_reduction_events",
                    "original_count": orig_events_count,
                    "retained_count": len(events),
                })
                staged_bytes = sum(f.stat().st_size for f in staging_dir.rglob("*") if f.is_file())

            # Stage 2: Strip request/response evidence in AI interactions
            if staged_bytes > TARGET_DIAGNOSTIC_BYTES and ai_list:
                for item in ai_list:
                    item["request_evidence"] = None
                    item["response_evidence"] = None
                (staging_dir / "ai-interactions.json").write_text(json.dumps(ai_list, indent=2), encoding="utf-8")
                if "ai-interactions.json" not in truncated_files:
                    truncated_files.append("ai-interactions.json")
                truncations_detail.append({
                    "file": "ai-interactions.json",
                    "stage": 2,
                    "reason": "stripped_ai_request_response_evidence",
                })
                staged_bytes = sum(f.stat().st_size for f in staging_dir.rglob("*") if f.is_file())

            # Stage 3: Truncate AI interaction rows to last 50
            if staged_bytes > TARGET_DIAGNOSTIC_BYTES and len(ai_list) > 50:
                ai_list = ai_list[-50:]
                (staging_dir / "ai-interactions.json").write_text(json.dumps(ai_list, indent=2), encoding="utf-8")
                if "ai-interactions.json" not in truncated_files:
                    truncated_files.append("ai-interactions.json")
                truncations_detail.append({
                    "file": "ai-interactions.json",
                    "stage": 3,
                    "reason": "ai_interaction_row_limit",
                    "original_count": orig_ai_count,
                    "retained_count": len(ai_list),
                })
                staged_bytes = sum(f.stat().st_size for f in staging_dir.rglob("*") if f.is_file())

            # Stage 4: Truncate optional research artifacts if budget exceeded
            if staged_bytes > TARGET_DIAGNOSTIC_BYTES:
                research_dir = staging_dir / "research"
                if research_dir.exists():
                    for res_name in ("grounding.json", "dossier.json", "audit.json"):
                        res_file = research_dir / res_name
                        if res_file.exists() and res_file.stat().st_size > 10000:
                            orig_res_size = res_file.stat().st_size
                            res_file.write_text(
                                json.dumps({"_notice": "[TRUNCATED RESEARCH DATA DUE TO SIZE BUDGET]"}, indent=2),
                                encoding="utf-8",
                            )
                            rel_name = f"research/{res_name}"
                            if rel_name not in truncated_files:
                                truncated_files.append(rel_name)
                            truncations_detail.append({
                                "file": rel_name,
                                "stage": 4,
                                "reason": "research_data_budget_reduction",
                                "original_bytes": orig_res_size,
                            })
                    staged_bytes = sum(f.stat().st_size for f in staging_dir.rglob("*") if f.is_file())

        # 13. manifest.json
        manifest_data = build_manifest_dict(job, db, included_files, truncated_files, truncations_detail)
        (staging_dir / "manifest.json").write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
        included_files.append("manifest.json")

        # Pre-send fail-closed secret scan on all staged artifacts
        for root, _, files in os.walk(staging_dir):
            for f in files:
                f_path = Path(root) / f
                content_bytes = f_path.read_bytes()
                leaks = scan_for_secrets(content_bytes)
                if leaks:
                    logger.error(
                        "Security violation: Diagnostics bundle failed pre-send secret scan in '%s'. Detected: %s",
                        f,
                        ", ".join(leaks),
                    )
                    raise RuntimeError("Security violation: Diagnostics bundle contained unredacted secret.")

        # Create ZIP Archive
        with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for root, _, files in os.walk(staging_dir):
                for f in files:
                    full_p = Path(root) / f
                    rel_p = full_p.relative_to(staging_dir)
                    zip_file.write(full_p, arcname=str(rel_p).replace("\\", "/"))

        # Pre-send size ceiling enforcement
        zip_size = tmp_zip_path.stat().st_size
        if zip_size > MAX_DIAGNOSTIC_BYTES:
            if tmp_zip_path.exists():
                tmp_zip_path.unlink()
            raise ValueError(f"Diagnostics bundle size ({zip_size} bytes) exceeds maximum allowable limit ({MAX_DIAGNOSTIC_BYTES} bytes).")

        # Pre-send fail-closed secret scan on raw zip bytes
        with open(tmp_zip_path, "rb") as zf:
            zip_bytes = zf.read()

        detected_leaks = scan_for_secrets(zip_bytes)
        if detected_leaks:
            if tmp_zip_path.exists():
                tmp_zip_path.unlink()
            logger.error("Security violation: Diagnostics bundle failed pre-send secret scan on ZIP archive. Detected: %s", ", ".join(detected_leaks))
            raise RuntimeError("Security violation: Diagnostics bundle contained unredacted secret.")

        # Atomic replace to final target path
        target_zip_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_zip_path, target_zip_path)
        logger.info(
            "Successfully generated diagnostics archive for job %s at %s (%d bytes)",
            job.id,
            target_zip_path,
            zip_size,
        )
        return target_zip_path

    finally:
        try:
            if tmp_zip_path.exists():
                tmp_zip_path.unlink()
        except Exception:
            pass
        try:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Failed to remove staging directory {staging_dir}: {e}")


def ensure_terminal_diagnostics_archive(
    job_id: str,
    expected_status: str | None = None,
) -> Path | None:
    """
    Ensure canonical diagnostics archive exists for a terminal job.
    Uses an isolated DB session so export failures never poison caller sessions.
    Terminal status is determined strictly from the database row.
    Safe against concurrent calls (idempotent, atomic file replacement).
    Returns canonical Path on success, None on error or if job is not terminal.
    """
    TERMINAL_STATES = {
        JobState.COMPLETE.value,
        JobState.FAILED_FINAL.value,
        JobState.CANCELLED.value,
    }

    session = SessionLocal()
    try:
        job = session.query(PodcastJob).filter(PodcastJob.id == job_id).first()
        if not job:
            logger.warning("ensure_terminal_diagnostics_archive: job %s not found", job_id)
            return None

        status = job.status
        if expected_status is not None and status != expected_status:
            logger.warning(
                "ensure_terminal_diagnostics_archive: job %s status is '%s', expected '%s'",
                job_id,
                status,
                expected_status,
            )
            return None

        if status not in TERMINAL_STATES:
            logger.debug(
                "ensure_terminal_diagnostics_archive: job %s status '%s' is not terminal, skipping",
                job_id,
                status,
            )
            return None

        canonical_path = get_terminal_diagnostics_path(job_id, status)
        if canonical_path.exists() and canonical_path.stat().st_size > 0:
            logger.debug(
                "ensure_terminal_diagnostics_archive: archive already exists for %s at %s",
                job_id,
                canonical_path,
            )
            return canonical_path

        generated_path = generate_job_diagnostics_zip(
            db=session,
            job=job,
            target_zip_path=canonical_path,
        )
        return generated_path
    except Exception as e:
        logger.error(
            "Failed to generate terminal diagnostics archive for job %s: %s",
            job_id,
            e,
            exc_info=True,
        )
        try:
            session.rollback()
        except Exception:
            pass
        return None
    finally:
        session.close()


def cleanup_expired_diagnostics_archives(retention_days: int | None = None) -> int:
    """
    Remove diagnostics archive files older than retention_days (default DIAGNOSTICS_RETENTION_DAYS).
    Also purges stale temporary .tmp.* files older than 1 hour.
    Returns the count of removed archive files.
    """
    if retention_days is None:
        retention_days = getattr(settings, "DIAGNOSTICS_RETENTION_DAYS", 30)

    base_dir = get_diagnostics_base_dir()
    if not base_dir.exists():
        return 0

    now = time.time()
    cutoff_time = now - (retention_days * 86400)
    tmp_cutoff_time = now - 3600  # 1 hour for abandoned temp files
    deleted_count = 0

    try:
        for entry in base_dir.iterdir():
            if not entry.is_file():
                continue

            # Clean stale temporary files
            if ".tmp." in entry.name:
                try:
                    if entry.stat().st_mtime < tmp_cutoff_time:
                        entry.unlink(missing_ok=True)
                        logger.debug("Cleaned stale diagnostics temp file: %s", entry.name)
                except Exception as ex:
                    logger.warning("Failed to remove stale temp file %s: %s", entry, ex)
                continue

            # Clean expired ZIP archives
            if entry.name.endswith(".zip"):
                try:
                    if entry.stat().st_mtime < cutoff_time:
                        entry.unlink(missing_ok=True)
                        deleted_count += 1
                        logger.debug(
                            "Removed expired diagnostics archive: %s (age > %d days)",
                            entry.name,
                            retention_days,
                        )
                except Exception as ex:
                    logger.warning("Failed to remove expired archive %s: %s", entry, ex)
    except Exception as e:
        logger.error("Error during diagnostics cleanup sweep: %s", e)

    if deleted_count > 0:
        logger.info(
            "Cleaned up %d expired diagnostics archives (>%d days old)",
            deleted_count,
            retention_days,
        )

    return deleted_count


def sweep_unarchived_terminal_jobs(
    db: Session,
    max_age_days: int | None = None,
    limit: int = 50,
) -> int:
    """
    Scan for recent terminal jobs that lack a persisted diagnostics archive and generate them.
    Bounded by limit to avoid overloading during startup or scheduled maintenance.
    """
    if max_age_days is None:
        max_age_days = getattr(settings, "DIAGNOSTICS_RETENTION_DAYS", 30)

    TERMINAL_STATES = [
        JobState.COMPLETE.value,
        JobState.FAILED_FINAL.value,
        JobState.CANCELLED.value,
    ]
    cutoff_dt = datetime.now(UTC) - timedelta(days=max_age_days)

    terminal_ts = func.coalesce(
        PodcastJob.completed_at,
        PodcastJob.updated_at,
        PodcastJob.created_at,
    )

    unarchived_jobs = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.status.in_(TERMINAL_STATES),
            terminal_ts >= cutoff_dt,
        )
        .order_by(terminal_ts.desc())
        .limit(limit)
        .all()
    )

    generated_count = 0
    for job in unarchived_jobs:
        archive_path = get_terminal_diagnostics_path(job.id, job.status)
        if not archive_path.exists():
            res = ensure_terminal_diagnostics_archive(job.id, job.status)
            if res:
                generated_count += 1

    if generated_count > 0:
        logger.info("Swept and generated %d missing terminal diagnostics archives", generated_count)

    return generated_count
