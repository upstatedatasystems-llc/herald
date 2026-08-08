import logging
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from herald.audio.ffmpeg_builder import (
    check_free_disk_mb,
    join_and_normalize_audio,
    validate_audio_file,
)
from herald.config import settings
from herald.db.connection import SessionLocal
from herald.db.models import JobState, PodcastJob
from herald.db.state_machine import transition_job_state
from herald.tts.chunker import chunk_podcast_script
from herald.tts.kokoro_client import (
    KokoroClient,
    KokoroTTSError,
    KokoroTTSTimeoutError,
)

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("herald.worker")


def slugify(text: str, max_len: int = 30) -> str:
    """Generate clean safe slug for audio filenames."""
    clean = "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")
    while "__" in clean:
        clean = clean.replace("__", "_")
    return clean[:max_len] or "episode"


def recover_stale_claims(db: Session, stale_minutes: int = 15):
    """
    Detect jobs stuck in SYNTHESIZING or ENCODING whose latest heartbeat/claim is past stale_minutes,
    and atomically requeue them back to QUEUED_TTS for retry.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_minutes)
    stale_jobs = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.status.in_([JobState.SYNTHESIZING.value, JobState.ENCODING.value]),
        )
        .with_for_update(skip_locked=True)
        .all()
    )

    for job in stale_jobs:
        last_active = job.last_heartbeat_at or job.claimed_at
        if last_active:
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=UTC)
            if last_active < cutoff:
                logger.warning(f"Recovering stale worker claim for job '{job.id}' (last active: {last_active})")
                job.claimed_at = None
                job.claim_owner = None
                job.last_heartbeat_at = None
                target_state = (
                    JobState.FAILED_FINAL.value if job.synthesis_attempt_count >= 3 else JobState.QUEUED_TTS.value
                )
                transition_job_state(
                    db,
                    job,
                    target_state,
                    component="herald-worker-recovery",
                    message="Recovered stale worker claim after crash or timeout",
                    force=True,
                    commit=False,
                )
                db.commit()


def process_next_job(db: Session, kokoro_client: KokoroClient) -> bool:
    """
    Acquire 1 QUEUED_TTS job using SELECT ... FOR UPDATE SKIP LOCKED in 1 transaction, synthesize TTS chunks,
    assemble with FFmpeg, and transition status to AUDIO_READY.
    """
    job = (
        db.query(PodcastJob)
        .filter(PodcastJob.status.in_([JobState.QUEUED_TTS.value, JobState.FAILED_RETRYABLE.value]))
        .order_by(PodcastJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )

    if not job:
        return False

    job.synthesis_attempt_count = (job.synthesis_attempt_count or 0) + 1
    if job.synthesis_attempt_count > 3:
        logger.error(f"Job '{job.id}' exceeded max synthesis attempts ({job.synthesis_attempt_count}).")
        job.error_code = "KOKORO_MAX_ATTEMPTS_EXCEEDED"
        job.error_detail = f"Exceeded maximum synthesis attempts ({job.synthesis_attempt_count})"
        transition_job_state(
            db,
            job,
            JobState.FAILED_FINAL.value,
            component="herald-worker",
            message="Exceeded max synthesis attempts",
            error_category="KOKORO_MAX_ATTEMPTS_EXCEEDED",
        )
        return False

    now = datetime.now(UTC)
    job.claimed_at = now
    job.claim_owner = "herald-worker"
    job.last_heartbeat_at = now
    transition_job_state(
        db,
        job,
        JobState.SYNTHESIZING.value,
        component="herald-worker",
        message="Claimed worker job atomically for TTS synthesis",
        commit=False,
    )
    db.commit()
    db.refresh(job)

    work_dir = Path(settings.HERALD_WORK_DIR)
    job_dir = work_dir / "jobs" / job.id
    chunks_dir = job_dir / "chunks"
    output_dir = work_dir / "output"

    chunks_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Low-disk space check before synthesis begins
        free_mb = check_free_disk_mb(work_dir)
        if free_mb < settings.HERALD_MIN_DISK_MB:
            raise KokoroTTSError(f"Low free disk space ({free_mb:.1f} MB available, required {settings.HERALD_MIN_DISK_MB} MB).")

        script = job.script_json or {}
        segments = script.get("segments", [])
        title = job.custom_title or script.get("episode_title", "Herald Episode")
        description = script.get("episode_description", "")

        voice = job.custom_voice or settings.KOKORO_VOICE
        speed = job.custom_speed if job.custom_speed is not None else settings.KOKORO_SPEED
        synthesis_timeout = getattr(settings, "KOKORO_SYNTHESIS_TIMEOUT_SECONDS", 180.0)

        if not segments:
            raise ValueError("Job script_json contains no segments to synthesize.")

        chunks = chunk_podcast_script(segments, max_chars=settings.TTS_MAX_CHUNK_CHARS)
        logger.info(f"Script split into {len(chunks)} TTS chunks for job '{job.id}' (Voice: {voice}, Speed: {speed}, Timeout: {synthesis_timeout}s)")

        generated_chunk_paths = []
        is_section_end_list = []

        for chunk in chunks:
            chunk_file = chunks_dir / f"chunk_{chunk.index:04d}.wav"
            is_section_end_list.append(chunk.is_section_end)

            if chunk.index <= job.completed_chunk_index and chunk_file.exists() and chunk_file.stat().st_size > 0:
                try:
                    validate_audio_file(chunk_file)
                    logger.info(f"Skipping already completed valid chunk {chunk.index}/{len(chunks)}")
                    generated_chunk_paths.append(chunk_file)
                    continue
                except Exception as ve:
                    logger.warning(f"Cached chunk {chunk.index} invalid ({ve}), re-synthesizing...")

            chunk_success = False
            last_chunk_error = None
            for chunk_attempt in range(1, 3):
                t0 = time.monotonic()
                try:
                    logger.info(
                        f"TTS chunk {chunk.index}/{len(chunks)} attempt {chunk_attempt}: "
                        f"{len(chunk.text)} chars | Kokoro timeout: {synthesis_timeout}s"
                    )
                    kokoro_client.synthesize_chunk(
                        text=chunk.text,
                        output_path=chunk_file,
                        voice=voice,
                        speed=speed,
                        timeout=synthesis_timeout,
                    )
                    validate_audio_file(chunk_file)
                    elapsed = time.monotonic() - t0
                    logger.info(
                        f"TTS chunk {chunk.index}/{len(chunks)} attempt {chunk_attempt}: "
                        f"Completed in {elapsed:.1f}s"
                    )
                    chunk_success = True
                    break
                except Exception as ce:
                    elapsed = time.monotonic() - t0
                    last_chunk_error = ce
                    if chunk_file.exists():
                        chunk_file.unlink(missing_ok=True)

                    is_timeout = isinstance(ce, KokoroTTSTimeoutError) or "timed out" in str(ce).lower()
                    if is_timeout:
                        logger.warning(
                            f"TTS chunk {chunk.index}/{len(chunks)} attempt {chunk_attempt} "
                            f"timed out after {elapsed:.1f}s"
                        )
                    else:
                        logger.warning(
                            f"TTS chunk {chunk.index}/{len(chunks)} attempt {chunk_attempt} "
                            f"failed in {elapsed:.1f}s: {ce}"
                        )
                    if chunk_attempt < 2:
                        time.sleep(1.0)

            if not chunk_success:
                is_timeout = isinstance(last_chunk_error, KokoroTTSTimeoutError) or "timed out" in str(last_chunk_error).lower()
                msg = f"TTS chunk {chunk.index}/{len(chunks)} failed after 2 attempts: {last_chunk_error}"
                if is_timeout:
                    raise KokoroTTSTimeoutError(msg)
                raise KokoroTTSError(msg)

            job.completed_chunk_index = chunk.index
            job.last_heartbeat_at = datetime.now(UTC)
            db.commit()
            generated_chunk_paths.append(chunk_file)

        transition_job_state(db, job, JobState.ENCODING.value, component="herald-worker")

        now_str = datetime.now(UTC).strftime("%Y-%m-%d_%H%M")
        title_slug = slugify(title)
        short_id = job.id[:8]
        mp3_filename = f"{now_str}_{title_slug}_{short_id}.mp3"
        output_mp3_path = output_dir / mp3_filename

        audio_info = None
        for ffmpeg_attempt in range(1, 3):
            try:
                audio_info = join_and_normalize_audio(
                    chunk_paths=generated_chunk_paths,
                    output_mp3_path=output_mp3_path,
                    episode_title=title,
                    episode_description=description,
                    job_id=job.id,
                    is_section_end_list=is_section_end_list,
                )
                break
            except Exception as fe:
                logger.warning(f"FFmpeg assembly attempt {ffmpeg_attempt} failed: {fe}")
                if output_mp3_path.exists():
                    output_mp3_path.unlink(missing_ok=True)
                if ffmpeg_attempt == 2:
                    raise

        job.local_audio_path = audio_info["output_path"]
        job.audio_bytes = audio_info["file_bytes"]
        job.audio_duration_seconds = audio_info["duration_seconds"]
        job.audio_sha256 = audio_info["sha256"]
        job.audio_ready_at = datetime.now(UTC)
        job.kokoro_voice = voice
        job.kokoro_speed = speed
        job.gemini_model = settings.GEMINI_MODEL
        db.commit()

        # Generate local artifacts
        try:
            from herald.audio.artifact_generator import (
                ensure_research_artifact,
                ensure_research_notes_artifact,
                ensure_script_artifact,
                ensure_source_artifact,
            )

            ensure_source_artifact(job, output_dir)
            ensure_script_artifact(job, output_dir)

            if (job.request_mode or "").lower() == "research" and job.research_json:
                ensure_research_artifact(job, output_dir)
                ensure_research_notes_artifact(job, output_dir)
        except Exception as se:
            logger.warning(f"Artifact creation warning for job '{job.id}': {se}")

        transition_job_state(db, job, JobState.AUDIO_READY.value, component="herald-worker")
        logger.info(f"Successfully rendered audio and source artifact for job '{job.id}': {output_mp3_path}")

        try:
            shutil.rmtree(chunks_dir)
        except Exception as e:
            logger.warning(f"Failed to remove temporary chunk directory '{chunks_dir}': {e}")

        return True

    except Exception as e:
        logger.error(f"Error processing job '{job.id}': {e}")
        job.attempt_count += 1
        
        is_timeout = isinstance(e, KokoroTTSTimeoutError) or "timed out" in str(e).lower()
        err_code = "KOKORO_SYNTHESIS_TIMEOUT" if is_timeout else "KOKORO_SYNTHESIS_FAILED"
        job.error_code = err_code
        job.error_detail = str(e)
        db.commit()

        target_failed_state = (
            JobState.FAILED_FINAL.value if job.synthesis_attempt_count >= 3 else JobState.FAILED_RETRYABLE.value
        )
        transition_job_state(
            db,
            job,
            target_failed_state,
            component="herald-worker",
            message=str(e),
            error_category=err_code,
        )
        return False


def run_worker_loop():
    """Main worker daemon loop."""
    logger.info("Starting Herald Worker daemon...")
    kokoro_client = KokoroClient()

    h_status = kokoro_client.health_check()
    logger.info(f"Startup Kokoro/FFmpeg Health Status: {h_status}")

    poll_interval = 5

    while True:
        try:
            db = SessionLocal()
            try:
                recover_stale_claims(db)
                job_processed = process_next_job(db, kokoro_client)
            finally:
                db.close()

            if not job_processed:
                time.sleep(poll_interval)
        except Exception as e:
            logger.error(f"Unexpected error in worker main loop: {e}")
            time.sleep(poll_interval)


if __name__ == "__main__":
    run_worker_loop()
