import math
from typing import Any

from sqlalchemy.orm import Session

from herald.config import settings
from herald.db.models import JobState, PodcastJob


def calculate_script_duration(script_json: dict, kokoro_speed: float = 1.0) -> dict[str, Any]:
    """
    Centralized programmatic duration & word count calculator.
    Uses NARRATION_WORDS_PER_MINUTE (default 136 WPM) baseline for Kokoro adjusted for speed.
    Returns dict with narration_word_count, predicted_duration_seconds, estimated_minutes.
    """
    if not script_json or not isinstance(script_json, dict):
        return {
            "narration_word_count": 0,
            "predicted_duration_seconds": 300,
            "estimated_minutes": 5,
        }

    segments = script_json.get("segments", [])
    total_words = 0
    for seg in segments:
        narration = seg.get("narration", "") if isinstance(seg, dict) else ""
        total_words += len(narration.split())

    wpm_base = getattr(settings, "NARRATION_WORDS_PER_MINUTE", 136)
    speed = float(kokoro_speed or 1.0)
    wpm_effective = wpm_base * speed

    # Add ~1.5s pause allowance per segment boundary
    pause_allowance_sec = len(segments) * 1.5
    predicted_seconds = int(round(((total_words / wpm_effective) * 60.0) + pause_allowance_sec)) if total_words > 0 else 300
    estimated_minutes = max(1, int(round(predicted_seconds / 60.0)))

    # Fallback to legacy estimated_minutes field if present without segments
    if not segments and "estimated_minutes" in script_json and script_json["estimated_minutes"]:
        estimated_minutes = int(script_json["estimated_minutes"])
        predicted_seconds = estimated_minutes * 60

    return {
        "narration_word_count": total_words,
        "predicted_duration_seconds": predicted_seconds,
        "estimated_minutes": estimated_minutes,
    }


def calculate_job_eta(db: Session, job: PodcastJob) -> dict[str, Any]:
    """
    Calculate approximate best-effort completion time for a podcast job.
    Uses weighted RTF from recent successful Kokoro request metrics when available.
    Counts only jobs created ahead of the target job in QUEUED_TTS, SYNTHESIZING, or ENCODING.
    """
    fallback_rtf = getattr(settings, "TTS_ESTIMATED_REALTIME_FACTOR", 2.4)
    overhead_seconds = getattr(settings, "DELIVERY_ESTIMATED_OVERHEAD_SECONDS", 60)

    # 0. Query recent successful TTS_TOTAL metrics and joined PodcastJob audio duration
    realtime_factor = fallback_rtf
    rtf_source = "fallback"

    try:
        from herald.db.models import JobProcessingMetric
        completed_jobs = (
            db.query(PodcastJob)
            .filter(
                PodcastJob.status == JobState.COMPLETE.value,
                PodcastJob.audio_duration_seconds.isnot(None),
                PodcastJob.audio_duration_seconds > 0,
            )
            .order_by(PodcastJob.completed_at.desc())
            .limit(20)
            .all()
        )
        if completed_jobs:
            job_durations = {j.id: j.audio_duration_seconds for j in completed_jobs}
            recent_tts_metrics = (
                db.query(JobProcessingMetric)
                .filter(
                    JobProcessingMetric.job_id.in_(list(job_durations.keys())),
                    JobProcessingMetric.stage == "TTS_TOTAL",
                    JobProcessingMetric.status == "success",
                    JobProcessingMetric.duration_ms > 0,
                )
                .all()
            )
            # Filter for metrics representing full synthesis (not cache-reuse retries)
            valid_metrics = [
                m for m in recent_tts_metrics
                if m.duration_ms and (m.metadata_json is None or (m.metadata_json or {}).get("full_synthesis") is not False)
            ]
            total_wall_ms = sum(m.duration_ms for m in valid_metrics if m.duration_ms)
            total_audio_ms = sum(job_durations[m.job_id] * 1000 for m in valid_metrics if m.job_id in job_durations)

            if total_audio_ms >= 10000 and total_wall_ms > 0:
                realtime_factor = round(total_wall_ms / float(total_audio_ms), 3)
                rtf_source = "historical"
    except Exception:
        pass

    # 1. Estimate duration for current job
    dur_info = calculate_script_duration(job.script_json, job.custom_speed or settings.KOKORO_SPEED)
    current_minutes = dur_info["estimated_minutes"]
    current_audio_seconds = float(dur_info["predicted_duration_seconds"])

    # 2. Estimate queue work ahead (jobs created before current_job)
    queue_ahead_audio_seconds = 0.0
    jobs_ahead_count = 0

    ahead_jobs = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.status.in_([
                JobState.QUEUED_TTS.value,
                JobState.SYNTHESIZING.value,
                JobState.ENCODING.value,
            ]),
            PodcastJob.id != job.id,
            PodcastJob.created_at < job.created_at,
        )
        .all()
    )

    for j in ahead_jobs:
        jobs_ahead_count += 1
        j_dur = calculate_script_duration(j.script_json, j.custom_speed or settings.KOKORO_SPEED)
        j_audio_seconds = j_dur["predicted_duration_seconds"]

        if j.status == JobState.SYNTHESIZING.value:
            from herald.db.models import PodcastTTSChunk
            total_chunks = (
                db.query(PodcastTTSChunk)
                .filter(PodcastTTSChunk.job_id == j.id)
                .count()
            )
            completed_chunks = (
                db.query(PodcastTTSChunk)
                .filter(
                    PodcastTTSChunk.job_id == j.id,
                    PodcastTTSChunk.status == "COMPLETED",
                )
                .count()
            )
            if total_chunks > 0:
                completed_ratio = min(1.0, max(0.0, completed_chunks / float(total_chunks)))
                remaining_audio_seconds = j_audio_seconds * (1.0 - completed_ratio)
                queue_ahead_audio_seconds += remaining_audio_seconds
            else:
                queue_ahead_audio_seconds += j_audio_seconds
        elif j.status == JobState.ENCODING.value:
            queue_ahead_audio_seconds += 10.0  # Encoding is almost complete
        else:
            queue_ahead_audio_seconds += j_audio_seconds

    predicted_tts_wall_time_seconds = int(round(current_audio_seconds * realtime_factor))
    estimated_remaining_processing_seconds = int(round((queue_ahead_audio_seconds + current_audio_seconds) * realtime_factor + overhead_seconds))

    total_eta_seconds = estimated_remaining_processing_seconds

    # Format human-friendly range
    eta_minutes = math.ceil(total_eta_seconds / 60.0)

    if eta_minutes <= 5:
        range_text = "approximately 3–5 minutes"
    elif eta_minutes <= 10:
        range_text = "approximately 5–10 minutes"
    elif eta_minutes <= 15:
        range_text = "approximately 10–15 minutes"
    elif eta_minutes <= 25:
        range_text = "approximately 15–25 minutes"
    elif eta_minutes <= 40:
        range_text = "approximately 25–40 minutes"
    else:
        lower = max(10, (eta_minutes // 10) * 10)
        upper = lower + 15
        range_text = f"approximately {lower}–{upper} minutes"

    return {
        "job_id": job.id,
        "estimated_minutes": current_minutes,
        "predicted_audio_duration_seconds": int(current_audio_seconds),
        "predicted_tts_wall_time_seconds": predicted_tts_wall_time_seconds,
        "estimated_remaining_processing_seconds": estimated_remaining_processing_seconds,
        "jobs_ahead": jobs_ahead_count,
        "total_eta_seconds": total_eta_seconds,
        "estimated_completion_range": range_text,
        "realtime_factor": realtime_factor,
        "rtf_source": rtf_source,
    }
