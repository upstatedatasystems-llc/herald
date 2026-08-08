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
        "diagnostics_filename": f"{base}_diagnostics.md",
        "research_filename": f"{base}_research.json",
        "research_notes_filename": f"{base}_research_notes.md",
    }


def get_required_artifact_types(job: PodcastJob) -> list[str]:
    """
    Central helper returning mode-aware list of required artifact keys for a job.
    Returns:
      - Brief / Standard: ['audio', 'source', 'script', 'diagnostics']
      - Research mode: ['audio', 'source', 'script', 'diagnostics', 'research', 'research_notes']
    """
    reqs = ["audio", "source", "diagnostics"]
    if job.script_json or (job.request_mode or "").lower() in ("brief", "standard", "research"):
        reqs.append("script")

    if (job.request_mode or "").lower() == "research":
        reqs.extend(["research", "research_notes"])

    return reqs


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
    Atomically generate or update local diagnostics Markdown artifact (<basename>_diagnostics.md).
    Builds a human-readable processing and audit report without Gemini API calls.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    names = get_artifact_filenames(job)
    diag_path = target_dir / names["diagnostics_filename"]

    script = job.script_json or {}
    segments = script.get("segments", [])
    warnings = script.get("warnings", [])
    dossier = job.research_json or {}
    audit = job.research_audit_json or {}

    created_iso = job.created_at.isoformat() if job.created_at else "N/A"
    audio_ready_iso = job.audio_ready_at.isoformat() if job.audio_ready_at else "N/A"
    completed_iso = job.completed_at.isoformat() if job.completed_at else "N/A"

    total_proc_seconds = None
    if job.created_at:
        start_t = job.created_at.replace(tzinfo=UTC) if job.created_at.tzinfo is None else job.created_at
        end_t = job.completed_at or datetime.now(UTC)
        end_t = end_t.replace(tzinfo=UTC) if end_t.tzinfo is None else end_t
        total_proc_seconds = round((end_t - start_t).total_seconds(), 2)

    source_words = len((job.source_text or "").split())
    dur_info = calculate_script_duration(script, job.kokoro_speed or job.custom_speed or 1.0)
    narration_words = dur_info["narration_word_count"]
    comp_ratio = round(narration_words / source_words, 2) if source_words > 0 else 0.0

    actual_duration = job.audio_duration_seconds
    actual_wpm = None
    if actual_duration and actual_duration > 0 and narration_words > 0:
        actual_wpm = round(narration_words / (actual_duration / 60.0), 2)

    is_research = (job.request_mode or "").lower() == "research"
    audit_pass = "PASS" if not audit.get("has_material_issues") else "FAIL (Repaired)"

    if actual_duration:
        mins, secs = divmod(actual_duration, 60)
        dur_str = f"{mins} minute(s) {secs} second(s) ({actual_duration}s)"
    else:
        dur_str = f"~{dur_info['estimated_minutes']} minute(s) (Predicted: {dur_info['predicted_duration_seconds']}s)"

    if job.audio_bytes:
        mb = round(job.audio_bytes / (1024 * 1024), 2)
        size_str = f"{mb} MB ({job.audio_bytes:,} bytes)"
    else:
        size_str = "N/A"

    lines = [
        "# Herald Run Diagnostics",
        "",
        "## Episode",
        f"- **Title**: {job.custom_title or script.get('episode_title', 'Herald Episode')}",
        f"- **Job ID**: `{job.id}`",
        f"- **Status**: `{job.status}`",
        f"- **Request Mode**: `{job.request_mode}`",
    ]
    if is_research:
        lines.append(f"- **Research Depth**: `{job.research_depth or 'medium'}`")
    lines.extend([
        f"- **Source Type**: `{job.source_type}`",
        f"- **Source Title / URL**: {job.source_url or job.custom_title or 'N/A'}",
        f"- **Created At**: {created_iso}",
        f"- **Completed At**: {completed_iso}",
        f"- **Total Processing Duration**: {total_proc_seconds}s" if total_proc_seconds else "- **Total Processing Duration**: N/A",
        "",
        "## Output Summary",
        f"- **Audio Duration**: {dur_str}",
        f"- **Audio File Size**: {size_str}",
        f"- **TTS Chunks**: {job.completed_chunk_index or 0}",
        f"- **Voice / Speed**: `{job.kokoro_voice or job.custom_voice or 'af_heart'}` @ `{job.kokoro_speed or job.custom_speed or 1.0}x`",
        f"- **Gemini Scripting Model**: `{job.gemini_model or 'gemini-3.5-flash'}`",
    ])
    if is_research:
        lines.extend([
            f"- **Research Model**: `{job.research_model or 'gemini-2.5-flash'}`",
            f"- **Search Query Count**: {job.research_search_count or 0}",
            f"- **Grounded Source Count**: {job.research_source_count or 0}",
            f"- **Script Repair Count**: {job.research_repair_count or 0}",
            f"- **Research Audit Status**: `{audit_pass}`",
        ])
    else:
        lines.append("- **Audit Status**: `PASS`")

    lines.extend([
        "",
        "## Content Metrics",
        f"- **Source Word Count**: {source_words}",
        f"- **Narration Word Count**: {narration_words}",
        f"- **Compression Ratio**: {comp_ratio}",
        f"- **Predicted Duration**: {dur_info['predicted_duration_seconds']}s (~{dur_info['estimated_minutes']} min)",
        f"- **Actual Duration**: {actual_duration}s" if actual_duration else "- **Actual Duration**: N/A",
        f"- **Actual Words Per Minute**: {actual_wpm} WPM" if actual_wpm else "- **Actual Words Per Minute**: N/A",
    ])

    if is_research:
        lines.extend(["", "## Research Summary"])
        sources = dossier.get("research_sources", [])
        queries = [s.get("search_query") for s in sources if s.get("search_query")]
        if queries:
            lines.append("**Search Queries Executed**:")
            for q in dict.fromkeys(queries):
                lines.append(f"- `{q}`")
        else:
            lines.append("- **Search Queries Executed**: None recorded")

        verifications = dossier.get("verification", [])
        if verifications:
            lines.append("\n**Claim Verifications**:")
            for v in verifications:
                s_ids = ", ".join(v.get("source_ids", []))
                lines.append(f"- **{v.get('source_claim')}** (`{v.get('status')}`): {v.get('notes')} [Sources: {s_ids}]")

        useful = dossier.get("useful_context", [])
        if useful:
            lines.append("\n**Useful Context Added**:")
            for u in useful:
                lines.append(f"- **{u.get('fact')}**: {u.get('why_it_matters')}")

        uncertain = dossier.get("outdated_or_uncertain", [])
        if uncertain:
            lines.append("\n**Contradictions & Discrepancies**:")
            for item in uncertain:
                lines.append(f"- {item}")

        lines.append(f"\n- **Audit Result**: `{audit_pass}`")
        lines.append(f"- **Targeted Script Repair Pass**: {'Executed' if job.research_repair_count else 'Not required'}")

    lines.extend(["", "## Original Source"])
    if job.source_text:
        lines.append(job.source_text.strip())
    else:
        lines.append("No source text recorded.")

    lines.extend(["", "## Final Podcast Script"])
    lines.append(f"### {script.get('episode_title', 'Untitled Episode')}")
    if script.get("episode_description"):
        lines.append(f"*{script.get('episode_description')}*\n")

    if segments:
        for seg in segments:
            lines.append(f"#### Segment {seg.get('order', 1)}: {seg.get('heading', '')}")
            lines.append(f"{seg.get('narration', '')}\n")
    else:
        lines.append("No script segments generated.")

    if warnings:
        lines.append("**Script Warnings**:")
        for w in warnings:
            lines.append(f"- {w}")

    if is_research:
        lines.extend(["", "## Research Sources"])
        sources = dossier.get("research_sources", [])
        if sources:
            for s in sources:
                lines.append(
                    f"1. **[{s.get('source_id', 'S')}] {s.get('title', 'Source')}** — [{s.get('domain', 'link')}]({s.get('url', '#')}) (Query: `{s.get('search_query', '')}`)"
                )
        else:
            lines.append("No grounded research sources recorded.")

    lines.extend([
        "",
        "## Audio / TTS",
        "- **Engine**: Kokoro TTS",
        f"- **Voice**: `{job.kokoro_voice or job.custom_voice or 'af_heart'}`",
        f"- **Speed**: `{job.kokoro_speed or job.custom_speed or 1.0}`",
        f"- **Chunk Count**: {job.completed_chunk_index or 0}",
        f"- **Synthesis Attempts**: {job.synthesis_attempt_count or 0}",
        f"- **Audio SHA-256**: `{job.audio_sha256 or 'N/A'}`",
        f"- **Final Duration**: {dur_str}",
        f"- **Final Byte Size**: {size_str}",
        f"- **Local Path**: `{job.local_audio_path or 'N/A'}`",
        "",
        "## Delivery",
        f"- **Drive Status**: {'Uploaded' if (job.drive_file_id and job.drive_web_link) else 'Pending'}",
        f"- **Email Status**: {'Delivered' if (job.delivered_at or job.gmail_result_message_id) else 'Pending'}",
        f"- **Delivery Attempt Count**: {job.delivery_attempt_count or 0}",
        "- **Drive Artifact Links / IDs**:",
        f"  - **Audio MP3**: {job.drive_web_link or 'N/A'} (ID: `{job.drive_file_id or 'N/A'}`)",
        f"  - **Source TXT**: {job.source_drive_web_link or 'N/A'} (ID: `{job.source_drive_file_id or 'N/A'}`)",
        f"  - **Script JSON**: {job.script_drive_web_link or 'N/A'} (ID: `{job.script_drive_file_id or 'N/A'}`)",
        f"  - **Diagnostics MD**: {job.diagnostics_drive_web_link or 'N/A'} (ID: `{job.diagnostics_drive_file_id or 'N/A'}`)",
    ])
    if is_research:
        lines.extend([
            f"  - **Research JSON**: {job.research_drive_web_link or 'N/A'} (ID: `{job.research_drive_file_id or 'N/A'}`)",
            f"  - **Research Notes MD**: {job.research_notes_drive_web_link or 'N/A'} (ID: `{job.research_notes_drive_file_id or 'N/A'}`)",
        ])

    lines.extend(["", "## Pipeline Timeline"])
    timeline_entries = []
    if hasattr(job, "transitions") and job.transitions:
        for t in sorted(job.transitions, key=lambda x: x.created_at or datetime.min):
            ts_str = t.created_at.isoformat() if t.created_at else ""
            msg = f" (`{t.message}`)" if t.message else ""
            timeline_entries.append(f"- **{ts_str}** — Transitioned to `{t.to_state}` by `{t.component}`{msg}")

    if not timeline_entries:
        if job.created_at:
            timeline_entries.append(f"- **{created_iso}** — Job received (Intake)")
        if job.audio_ready_at:
            timeline_entries.append(f"- **{audio_ready_iso}** — Audio synthesis completed")
        if job.drive_uploaded_at:
            timeline_entries.append(f"- **{job.drive_uploaded_at.isoformat()}** — Google Drive artifacts uploaded")
        if job.delivered_at:
            timeline_entries.append(f"- **{job.delivered_at.isoformat()}** — Completion email delivered")
        if job.completed_at:
            timeline_entries.append(f"- **{completed_iso}** — Job state COMPLETE")

    lines.extend(timeline_entries if timeline_entries else ["No timeline transitions recorded."])

    lines.extend(["", "## Errors and Warnings"])
    if job.error_code or job.error_detail:
        lines.append(f"- **Errors**: `{job.error_code}` — {job.error_detail}")
    else:
        lines.append("- **Errors**: None")

    if warnings:
        lines.append("- **Warnings**:")
        for w in warnings:
            lines.append(f"  - {w}")
    else:
        lines.append("- **Warnings**: None")

    lines.extend([
        "",
        "## Technical Identifiers",
        f"- **Gmail Message ID**: `{job.gmail_message_id or 'N/A'}`",
        f"- **Gmail Thread ID**: `{job.gmail_thread_id or 'N/A'}`",
        f"- **Source Hash**: `{job.source_hash or 'N/A'}`",
        f"- **Audio SHA-256**: `{job.audio_sha256 or 'N/A'}`",
        f"- **Drive Job Key**: `{job.drive_job_key or 'N/A'}`",
    ])

    content = "\n".join(lines)
    tmp_path = diag_path.with_suffix(".md.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)

    os.replace(tmp_path, diag_path)
    return diag_path
