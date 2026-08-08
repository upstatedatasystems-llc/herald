import json
import os
from datetime import UTC, datetime
from pathlib import Path

from herald.db.models import PodcastJob, SourceType
from herald.services.eta_calculator import calculate_script_duration


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
    """Return canonical filenames for all job artifacts."""
    base = get_job_basename(job)
    return {
        "basename": base,
        "audio_filename": f"{base}.mp3",
        "source_filename": f"{base}_source.txt",
        "script_filename": f"{base}_script.json",
        "diagnostics_filename": f"{base}_diagnostics.json",
        "research_filename": f"{base}_research.json",
        "research_notes_filename": f"{base}_research_notes.md",
    }


def ensure_source_artifact(job: PodcastJob, target_dir: Path) -> Path:
    """
    Atomically generate or verify local source text artifact (<basename>_source.txt).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    names = get_artifact_filenames(job)
    source_path = target_dir / names["source_filename"]

    if source_path.exists() and source_path.stat().st_size > 0:
        return source_path

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

    tmp_path = source_path.with_suffix(".txt.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)

    os.replace(tmp_path, source_path)
    return source_path


def ensure_script_artifact(job: PodcastJob, target_dir: Path) -> Path:
    """
    Atomically generate local script JSON artifact (<basename>_script.json).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    names = get_artifact_filenames(job)
    script_path = target_dir / names["script_filename"]

    data = job.script_json or {}
    tmp_path = script_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    os.replace(tmp_path, script_path)
    return script_path


def ensure_research_artifact(job: PodcastJob, target_dir: Path) -> Path:
    """
    Atomically generate machine-readable research JSON artifact (<basename>_research.json).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    names = get_artifact_filenames(job)
    res_path = target_dir / names["research_filename"]

    data = job.research_json or {}
    tmp_path = res_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    os.replace(tmp_path, res_path)
    return res_path


def ensure_research_notes_artifact(job: PodcastJob, target_dir: Path) -> Path:
    """
    Atomically generate human-readable Markdown Research Notes artifact (<basename>_research_notes.md).
    Includes summary, verification, context, uncertainty, numbered source list, search metrics.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    names = get_artifact_filenames(job)
    notes_path = target_dir / names["research_notes_filename"]

    dossier = job.research_json or {}
    script = job.script_json or {}
    title = job.custom_title or script.get("episode_title") or "Herald Research Episode"
    depth = (job.research_depth or "medium").capitalize()

    lines = [
        f"# Research Notes: {title}",
        f"**Research Depth**: {depth}",
        f"**Research Model**: {job.research_model or 'gemini-2.5-flash'}",
        f"**Total Search Queries**: {job.research_search_count or 0}",
        f"**Unique Grounded Sources**: {job.research_source_count or 0}",
        f"**Script Repair Pass**: {'Executed' if job.research_repair_count else 'None'}",
        "",
        "## Investigation Summary",
        dossier.get("source_summary", "No summary provided."),
        "",
        "## Claim Verification",
    ]

    verifications = dossier.get("verification", [])
    if verifications:
        for v in verifications:
            s_ids = ", ".join(v.get("source_ids", [])) or "N/A"
            lines.append(f"- **Claim**: {v.get('source_claim')}")
            lines.append(f"  - **Status**: `{v.get('status')}`")
            lines.append(f"  - **Notes**: {v.get('notes')}")
            lines.append(f"  - **Sources**: {s_ids}")
    else:
        lines.append("No specific claim verifications recorded.")

    lines.extend(["", "## Additional Context & Updates"])
    useful = dossier.get("useful_context", [])
    if useful:
        for u in useful:
            s_ids = ", ".join(u.get("source_ids", [])) or "N/A"
            lines.append(f"- **Fact**: {u.get('fact')}")
            lines.append(f"  - **Why it matters**: {u.get('why_it_matters')}")
            lines.append(f"  - **Sources**: {s_ids}")
    else:
        lines.append("No additional context recorded.")

    lines.extend(["", "## Discrepancies & Uncertainties"])
    uncertain = dossier.get("outdated_or_uncertain", [])
    if uncertain:
        for item in uncertain:
            lines.append(f"- {item}")
    else:
        lines.append("No material discrepancies or unresolved uncertainties detected.")

    lines.extend(["", "## Grounded Research Sources"])
    sources = dossier.get("research_sources", [])
    if sources:
        for s in sources:
            sid = s.get("source_id", "")
            s_title = s.get("title", "Source")
            url = s.get("url", "#")
            domain = s.get("domain", "")
            lines.append(f"1. **[{sid}] [{s_title}]({url})** — *{domain}* (Query: `{s.get('search_query', '')}`)")
    else:
        lines.append("No external sources recorded.")

    content = "\n".join(lines)
    tmp_path = notes_path.with_suffix(".md.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)

    os.replace(tmp_path, notes_path)
    return notes_path


def generate_diagnostics_artifact(job: PodcastJob, target_dir: Path) -> Path:
    """
    Atomically generate or update local diagnostics JSON artifact (<basename>_diagnostics.json).
    Includes enhanced benchmarking metrics (words per minute, compression ratio, predicted vs actual duration).
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

    source_words = len((job.source_text or "").split())
    dur_info = calculate_script_duration(script, job.kokoro_speed or job.custom_speed or 1.0)
    narration_words = dur_info["narration_word_count"]
    comp_ratio = round(narration_words / source_words, 2) if source_words > 0 else 0.0

    actual_duration = job.audio_duration_seconds
    actual_wpm = None
    if actual_duration and actual_duration > 0 and narration_words > 0:
        actual_wpm = round(narration_words / (actual_duration / 60.0), 2)

    diag_data = {
        "job_id": job.id,
        "gmail_message_id": job.gmail_message_id,
        "request_mode": job.request_mode,
        "research_depth": job.research_depth,
        "source_type": job.source_type,
        "source_url": job.source_url,
        "source_hash": job.source_hash,
        "metrics": {
            "source_word_count": source_words,
            "narration_word_count": narration_words,
            "compression_ratio": comp_ratio,
            "predicted_duration_seconds": dur_info["predicted_duration_seconds"],
            "actual_duration_seconds": actual_duration,
            "actual_words_per_minute": actual_wpm,
            "research_search_count": job.research_search_count,
            "research_source_count": job.research_source_count,
            "research_repair_count": job.research_repair_count,
        },
        "episode": {
            "title": job.custom_title or script.get("episode_title"),
            "description": script.get("episode_description"),
            "estimated_minutes": dur_info["estimated_minutes"],
            "actual_duration_seconds": actual_duration,
            "script_segments": len(segments),
            "script_warnings": warnings,
        },
        "generation": {
            "gemini_model": job.gemini_model or "gemini-3.5-flash",
            "research_model": job.research_model,
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
            "script_file_id": job.script_drive_file_id,
            "research_file_id": job.research_drive_file_id,
            "research_notes_file_id": job.research_notes_drive_file_id,
        },
    }

    tmp_path = diag_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(diag_data, f, indent=2, ensure_ascii=False)

    os.replace(tmp_path, diag_path)
    return diag_path
