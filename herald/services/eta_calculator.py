import math
from datetime import UTC, datetime
from sqlalchemy.orm import Session

from herald.config import settings
from herald.db.models import JobState, PodcastJob


def calculate_script_duration(script_json: dict, kokoro_speed: float = 1.0) -> dict:
    """
    Centralized programmatic duration & word count calculator.
    Uses ~150 WPM baseline for Kokoro at speed 1.0.
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

    speed = kokoro_speed if kokoro_speed and kokoro_speed > 0 else 1.0
    wpm = 150.0 * speed
    predicted_seconds = int(round((total_words / wpm) * 60.0)) if total_words > 0 else 300
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


def calculate_job_eta(db: Session, job: PodcastJob) -> dict[str, any]:
    """
    Calculate approximate best-effort completion time for a podcast job.
    Counts only jobs created ahead of the target job in QUEUED_TTS, SYNTHESIZING, or ENCODING.
    """
    realtime_factor = settings.TTS_ESTIMATED_REALTIME_FACTOR
    overhead_seconds = settings.DELIVERY_ESTIMATED_OVERHEAD_SECONDS

    # 1. Estimate duration for current job
    dur_info = calculate_script_duration(job.script_json, job.custom_speed or settings.KOKORO_SPEED)
    current_minutes = dur_info["estimated_minutes"]
    current_audio_seconds = current_minutes * 60.0

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

        if j.status == JobState.SYNTHESIZING.value and j.completed_chunk_index > 0:
            segments = (j.script_json or {}).get("segments", [])
            total_segments = max(len(segments), 1)
            completed_ratio = min(1.0, max(0.0, j.completed_chunk_index / total_segments))
            remaining_audio_seconds = j_audio_seconds * (1.0 - completed_ratio)
            queue_ahead_audio_seconds += remaining_audio_seconds
        elif j.status == JobState.ENCODING.value:
            queue_ahead_audio_seconds += 10.0  # Encoding is almost complete
        else:
            queue_ahead_audio_seconds += j_audio_seconds

    total_synthesis_seconds = (queue_ahead_audio_seconds + current_audio_seconds) * realtime_factor
    total_eta_seconds = int(total_synthesis_seconds + overhead_seconds)

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
        "jobs_ahead": jobs_ahead_count,
        "total_eta_seconds": total_eta_seconds,
        "estimated_completion_range": range_text,
        "realtime_factor": realtime_factor,
    }
