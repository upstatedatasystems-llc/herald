import json
import os
from datetime import UTC, datetime
from pathlib import Path

from herald.db.models import PodcastJob, SourceType


def get_job_basename(job: PodcastJob) -> str:
    """Generate central sanitized basename for all job artifacts."""
    if job.local_audio_path:
        stem = Path(job.local_audio_path).stem
        if stem:
            return stem

    now_str = (job.audio_ready_at or job.created_at or datetime.now(UTC)).strftime("%Y-%m-%d_%H%M")
    script = job.script_json or {}
    title = job.custom_title or script.get("episode_title", "herald_episode")
    clean_title = "".join(c if c.isalnum() else "_" for c in title.lower()).strip("_")
    while "__" in clean_title:
        clean_title = clean_title.replace("__", "_")
    slug = clean_title[:30] or "episode"
    short_id = job.id[:8]
    return f"{now_str}_{slug}_{short_id}"


def get_artifact_filenames(job: PodcastJob) -> dict[str, str]:
    """Return canonical filenames for all 3 job artifacts."""
    base = get_job_basename(job)
    return {
        "basename": base,
        "audio_filename": f"{base}.mp3",
        "source_filename": f"{base}_source.txt",
        "diagnostics_filename": f"{base}_diagnostics.json",
    }


def ensure_source_artifact(job: PodcastJob, target_dir: Path) -> Path:
    """
    Atomically generate or verify local source text artifact (<basename>_source.txt).
    Deterministic & regenerable on demand from job.source_text.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    names = get_artifact_filenames(job)
    source_path = target_dir / names["source_filename"]

    if source_path.exists() and source_path.stat().st_size > 0:
        return source_path

    # Construct source content
    if job.source_type == SourceType.URL.value and job.source_url:
        created_str = (job.created_at or datetime.now(UTC)).isoformat()
        script = job.script_json or {}
        source_title = job.custom_title or script.get("episode_title") or "Unknown Title"

        header = (
            f"Herald Source Material\n"
            f"Source URL: {job.source_url}\n"
            f"Source Title: {source_title}\n"
            f"Job ID: {job.id}\n"
            f"Retrieved At: {created_str}\n"
            f"{'=' * 50}\n\n"
        )
        content = header + (job.source_text or "")
    else:
        content = job.source_text or ""

    # Atomic write to temp file then rename
    tmp_path = source_path.with_suffix(".txt.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)

    os.replace(tmp_path, source_path)
    return source_path


def generate_diagnostics_artifact(job: PodcastJob, target_dir: Path) -> Path:
    """
    Atomically generate or update local diagnostics JSON artifact (<basename>_diagnostics.json).
    Dynamic metadata created during delivery step. Excludes secrets and pending diagnostics_drive_file_id.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    names = get_artifact_filenames(job)
    diag_path = target_dir / names["diagnostics_filename"]

    script = job.script_json or {}
    segments = script.get("segments", [])
    warnings = script.get("warnings", [])

    created_iso = job.created_at.isoformat() if job.created_at else None
    audio_ready_iso = job.audio_ready_at.isoformat() if job.audio_ready_at else None
    now_iso = datetime.now(UTC).isoformat()

    total_proc_seconds = None
    if job.created_at:
        start_t = job.created_at.replace(tzinfo=UTC) if job.created_at.tzinfo is None else job.created_at
        total_proc_seconds = round((datetime.now(UTC) - start_t).total_seconds(), 2)

    diag_data = {
        "job_id": job.id,
        "gmail_message_id": job.gmail_message_id,
        "request_mode": job.request_mode,
        "source_type": job.source_type,
        "source_url": job.source_url,
        "source_hash": job.source_hash,
        "episode": {
            "title": job.custom_title or script.get("episode_title"),
            "description": script.get("episode_description"),
            "estimated_minutes": script.get("estimated_minutes"),
            "actual_duration_seconds": job.audio_duration_seconds,
            "script_segments": len(segments),
            "script_warnings": warnings,
        },
        "generation": {
            "gemini_model": job.gemini_model or "gemini-3.5-flash",
            "kokoro_voice": job.kokoro_voice or job.custom_voice or "af_heart",
            "kokoro_speed": job.kokoro_speed or job.custom_speed or 1.0,
            "completed_chunk_index": job.completed_chunk_index or 0,
            "retry_attempts": max(0, job.attempt_count or 0),
            "synthesis_attempt_count": job.synthesis_attempt_count or 0,
            "delivery_attempt_count": job.delivery_attempt_count or 0,
        },
        "audio": {
            "bytes": job.audio_bytes,
            "sha256": job.audio_sha256,
            "format": "mp3",
        },
        "timing": {
            "created_at": created_iso,
            "audio_ready_at": audio_ready_iso,
            "diagnostics_generated_at": now_iso,
            "total_processing_seconds": total_proc_seconds,
        },
        "drive": {
            "audio_file_id": job.drive_file_id,
            "source_file_id": job.source_drive_file_id,
        },
    }

    tmp_path = diag_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(diag_data, f, indent=2, ensure_ascii=False)

    os.replace(tmp_path, diag_path)
    return diag_path
