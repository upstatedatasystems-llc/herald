import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from sqlalchemy.orm import Session

from herald.db.connection import SessionLocal
from herald.db.models import JobProcessingMetric, PodcastJob, SourceType
from herald.services.eta_calculator import calculate_script_duration

logger = logging.getLogger("herald.artifact_generator")


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


def ensure_source_artifact(job: PodcastJob, output_dir: Path) -> Path:
    """Legacy helper stub pointing to unified details artifact generator."""
    return ensure_details_artifact(job, output_dir)


def ensure_script_artifact(job: PodcastJob, output_dir: Path) -> Path:
    """Legacy helper stub pointing to unified details artifact generator."""
    return ensure_details_artifact(job, output_dir)


def ensure_research_artifact(job: PodcastJob, output_dir: Path) -> Path:
    """Legacy helper stub pointing to unified details artifact generator."""
    return ensure_details_artifact(job, output_dir)


def ensure_research_notes_artifact(job: PodcastJob, output_dir: Path) -> Path:
    """Legacy helper stub pointing to unified details artifact generator."""
    return ensure_details_artifact(job, output_dir)


def generate_diagnostics_artifact(job: PodcastJob, output_dir: Path) -> Path:
    """Legacy helper stub pointing to unified details artifact generator."""
    return ensure_details_artifact(job, output_dir)



def get_artifact_filenames(job: PodcastJob) -> dict[str, str]:
    """Return canonical filenames for all job artifacts."""
    base = get_job_basename(job)
    return {
        "basename": base,
        "audio_filename": f"{base}.mp3",
        "details_filename": f"{base}_details.md",
    }


def get_required_artifact_types(job: PodcastJob) -> list[str]:
    """
    Central helper returning mode-aware list of required artifact keys for a job.
    In Phase 1, ALL job modes produce exactly TWO primary Drive artifacts: audio and details.
    """
    return ["audio", "details"]


def ensure_details_artifact(job: PodcastJob, target_dir: Path, db: Session | None = None) -> Path:
    """
    Atomically generate or regenerate unified local Markdown companion artifact (<basename>_details.md).
    Consolidates episode metadata, processing & performance summary, content metrics,
    original source text, final podcast script (rendered Markdown + fenced script_json),
    pipeline timeline, errors/warnings, research notes & dossiers (for research jobs), and technical IDs.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    names = get_artifact_filenames(job)
    details_path = target_dir / names["details_filename"]

    script = job.script_json or {}
    segments = script.get("segments", [])
    warnings = script.get("warnings", [])
    dossier = job.research_json or {}
    audit = job.research_audit_json or {}
    is_research = (job.request_mode or "").lower() == "research"

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

    # Query metrics safely for performance summary table
    stage_metrics_map: dict[str, Any] = {}
    m_rows = []
    if db is not None:
        try:
            m_rows = db.query(JobProcessingMetric).filter(JobProcessingMetric.job_id == job.id).all()
        except Exception as e:
            logger.warning(f"Could not load performance metrics from passed db: {e}")

    if not m_rows and hasattr(job, "metrics") and job.metrics:
        try:
            m_rows = list(job.metrics)
        except Exception:
            pass

    if not m_rows:
        try:
            db_m = SessionLocal()
            try:
                m_rows = (
                    db_m.query(JobProcessingMetric)
                    .filter(JobProcessingMetric.job_id == job.id)
                    .all()
                )
            finally:
                db_m.close()
        except Exception as me:
            logger.warning(f"Could not load performance metrics for details artifact generation: {me}")

    for r in m_rows:
        stage_metrics_map[r.stage] = r

    lines = [
        "# Herald Episode Details",
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
        f"- **Audio Ready At**: {audio_ready_iso}",
        f"- **Completed At**: {completed_iso}",
        f"- **Details Finalized At**: {job.details_finalized_at.isoformat() if getattr(job, 'details_finalized_at', None) else 'N/A'}",
        "",
        "## Processing Summary",
        f"- **Overall Status**: `{job.status}`",
        f"- **Gemini Scripting Model**: `{job.gemini_model or 'gemini-3.5-flash'}`",
        f"- **Kokoro Voice / Speed**: `{job.kokoro_voice or job.custom_voice or 'af_heart'}` @ `{job.kokoro_speed or job.custom_speed or 1.0}x`",
        f"- **Configured TTS Chunk Size**: `{getattr(job, 'tts_chunk_chars', 500) or 500} chars`",
        f"- **Script Verification Setting**: `{bool(getattr(job, 'verify_final_script', False))}`",
        f"- **Audio Duration**: {dur_str}",
        f"- **Audio File Size**: {size_str}",
        f"- **Audio SHA-256**: `{job.audio_sha256 or 'N/A'}`",
        f"- **TTS Chunks Count**: {job.completed_chunk_index or 0}",
        f"- **Synthesis Attempt Count**: {job.synthesis_attempt_count or 0}",
        f"- **Total Processing Time**: {total_proc_seconds}s" if total_proc_seconds else "- **Total Processing Time**: N/A",
    ])

    v_audit = getattr(job, "verify_audit_json", None) or {}
    v_rep_count = getattr(job, "verify_repair_count", 0) or 0
    if getattr(job, "verify_final_script", False):
        v_status = "PASS" if not v_audit.get("has_material_issues") else f"REPAIRED ({v_rep_count} pass)"
        lines.append(f"- **Fidelity Verify Status**: `{v_status}`")

    if is_research:
        lines.extend([
            f"- **Research Model**: `{job.research_model or 'gemini-2.5-flash'}`",
            f"- **Search Queries Executed**: {job.research_search_count or 0}",
            f"- **Grounded Sources Count**: {job.research_source_count or 0}",
            f"- **Research Repair Count**: {job.research_repair_count or 0}",
            f"- **Research Audit Status**: `{audit_pass}`",
        ])

    # TTS Resource Monitoring aggregates table if collected
    r_metrics = getattr(job, "tts_resource_metrics_json", None)
    if r_metrics and isinstance(r_metrics, dict) and r_metrics.get("sample_count", 0) > 0:
        lines.extend([
            "",
            "### TTS Resource Monitoring Summary (Synthesizing Window)",
            f"- **Sample Count**: {r_metrics.get('sample_count', 0)} (Interval: ~{r_metrics.get('sample_interval_seconds', 5.0)}s)",
            f"- **Observed TTS Wall Time**: {r_metrics.get('observed_tts_wall_time_ms', 0)} ms ({r_metrics.get('observed_tts_wall_time_ms', 0) / 1000.0:.2f}s)",
            f"- **CPU Utilization**: Avg `{r_metrics.get('avg_cpu_percent', 0.0)}%` | Peak `{r_metrics.get('peak_cpu_percent', 0.0)}%`",
            f"- **Process Memory Peak**: `{r_metrics.get('peak_memory_mb', 0.0)} MB`",
            f"- **Min Available System Memory**: `{r_metrics.get('minimum_available_memory_mb', 0.0)} MB`",
            f"- **Swap Usage**: Start `{r_metrics.get('swap_start_mb', 0.0)} MB` | End `{r_metrics.get('swap_end_mb', 0.0)} MB` | Peak `{r_metrics.get('swap_peak_mb', 0.0)} MB`",
        ])

    # Add Performance Metrics subsection if data is present
    if stage_metrics_map:
        lines.extend(["", "### Performance Metrics Summary"])
        metric_order = [
            ("EMAIL_DETECTION_WAIT", "Email Detection Wait"),
            ("INTAKE_TOTAL", "Intake Total"),
            ("URL_EXTRACTION", "URL Extraction"),
            ("GEMINI_SCRIPT", "Gemini Script Generation"),
            ("RESEARCH_GROUNDING", "Research Grounding"),
            ("RESEARCH_NORMALIZATION", "Research Normalization"),
            ("RESEARCH_SCRIPT", "Research Scripting"),
            ("RESEARCH_AUDIT", "Research Audit"),
            ("RESEARCH_REPAIR", "Research Repair"),
            ("VERIFY_AUDIT", "Verify Script Audit"),
            ("VERIFY_REPAIR", "Verify Script Repair"),
            ("TTS_QUEUE_WAIT", "TTS Queue Wait"),
            ("TTS_CHUNKING", "TTS Chunking"),
            ("TTS_TOTAL", "TTS Total Synthesis"),
            ("FFMPEG_ENCODING", "FFmpeg Assembly"),
            ("DELIVERY_DISPATCH_WAIT", "Delivery Dispatch Wait"),
            ("DRIVE_AUDIO_UPLOAD", "Drive Audio Upload"),
            ("DRIVE_DETAILS_UPLOAD", "Drive Details Upload"),
            ("EMAIL_DELIVERY", "Email Delivery"),
            ("DRIVE_DETAILS_FINALIZE", "Drive Details In-Place Finalize"),
        ]
        for stage_key, label in metric_order:
            if stage_key in stage_metrics_map:
                m = stage_metrics_map[stage_key]
                dur_txt = f"{m.duration_ms} ms ({m.duration_ms / 1000.0:.2f}s)" if m.duration_ms is not None else "N/A"
                lines.append(f"- **{label}**: {dur_txt} [`{m.status}`]")

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
        lines.extend(["", "## Research Investigation Summary"])
        lines.append(dossier.get("source_summary", "No research summary recorded."))

        lines.extend(["", "## Claim Verification"])
        verifications = dossier.get("verification", [])
        if verifications:
            for v in verifications:
                s_ids = ", ".join(v.get("source_ids", [])) or "N/A"
                lines.append(f"- **Claim**: {v.get('source_claim')}")
                lines.append(f"  - **Status**: `{v.get('status')}`")
                lines.append(f"  - **Notes**: {v.get('notes')}")
                lines.append(f"  - **Sources**: {s_ids}")
        else:
            lines.append("No claim verifications recorded.")

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
                sid = s.get("source_id", "S")
                stitle = s.get("title", "Source")
                surl = s.get("url", "#")
                domain = s.get("domain", "")
                query = s.get("search_query", "")
                lines.append(f"1. **[{sid}] [{stitle}]({surl})** — *{domain}* (Query: `{query}`)")
        else:
            lines.append("No grounded research sources recorded.")

    lines.extend(["", "## Original Source"])
    if job.source_type == SourceType.URL.value and job.source_url:
        created_str = (job.created_at or datetime.now(UTC)).isoformat()
        source_title = job.custom_title or script.get("episode_title") or "Unknown Title"
        lines.extend([
            f"**Source URL**: {job.source_url}",
            f"**Source Title**: {source_title}",
            f"**Retrieved At**: {created_str}",
            "---",
            "",
        ])
    lines.append(job.source_text.strip() if job.source_text else "No source text recorded.")

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

    # Preserve full structured script JSON in a fenced section
    lines.extend([
        "",
        "### Structured Script JSON",
        "```json",
        json.dumps(script, indent=2, ensure_ascii=False),
        "```",
    ])

    if is_research and job.research_json:
        lines.extend([
            "",
            "### Structured Research Dossier JSON",
            "```json",
            json.dumps(job.research_json, indent=2, ensure_ascii=False),
            "```",
        ])

    if is_research and job.research_audit_json:
        lines.extend([
            "",
            "### Research Audit JSON",
            "```json",
            json.dumps(job.research_audit_json, indent=2, ensure_ascii=False),
            "```",
        ])

    lines.extend(["", "## Pipeline Transition Timeline"])
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
        f"- **Job ID**: `{job.id}`",
        f"- **Gmail Message ID**: `{job.gmail_message_id or 'N/A'}`",
        f"- **Gmail Thread ID**: `{job.gmail_thread_id or 'N/A'}`",
        f"- **Source Hash**: `{job.source_hash or 'N/A'}`",
        f"- **Audio SHA-256**: `{job.audio_sha256 or 'N/A'}`",
        f"- **Drive Job Key**: `{job.drive_job_key or 'N/A'}`",
        f"- **Audio Drive File ID**: `{job.drive_file_id or 'N/A'}`",
        f"- **Details Drive File ID**: `{job.details_drive_file_id or 'N/A'}`",
    ])

    content = "\n".join(lines)
    tmp_path = details_path.with_suffix(".md.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)

    os.replace(tmp_path, details_path)
    return details_path
