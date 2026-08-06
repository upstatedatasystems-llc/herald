import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from packages.herald.audio.ffmpeg_builder import join_and_normalize_audio
from packages.herald.config import settings
from packages.herald.db.connection import SessionLocal
from packages.herald.db.models import JobState, PodcastJob
from packages.herald.db.state_machine import transition_job_state
from packages.herald.tts.chunker import chunk_podcast_script
from packages.herald.tts.kokoro_client import KokoroClient

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


def process_next_job(db: Session, kokoro_client: KokoroClient) -> bool:
    """
    Acquire 1 QUEUED job using SELECT ... FOR UPDATE SKIP LOCKED, synthesize TTS chunks,
    assemble with FFmpeg, and transition status to AUDIO_READY.
    Returns True if a job was processed, False otherwise.
    """
    # Exclusive database lock to acquire single job safely
    job = (
        db.query(PodcastJob)
        .filter(PodcastJob.status == JobState.QUEUED.value)
        .order_by(PodcastJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )

    if not job:
        return False

    logger.info(f"Worker claimed job ID: '{job.id}' (Message ID: '{job.gmail_message_id}')")

    transition_job_state(db, job, JobState.SYNTHESIZING.value, component="herald-worker")

    work_dir = Path(settings.HERALD_WORK_DIR)
    job_dir = work_dir / "jobs" / job.id
    chunks_dir = job_dir / "chunks"
    output_dir = work_dir / "output"

    chunks_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        script = job.script_json or {}
        segments = script.get("segments", [])
        title = script.get("episode_title", "Herald Episode")
        description = script.get("episode_description", "")

        if not segments:
            raise ValueError("Job script_json contains no segments to synthesize.")

        chunks = chunk_podcast_script(segments, max_chars=settings.TTS_MAX_CHUNK_CHARS)
        logger.info(f"Script split into {len(chunks)} TTS chunks for job '{job.id}'")

        generated_chunk_paths = []

        for chunk in chunks:
            chunk_file = chunks_dir / f"chunk_{chunk.index:04d}.wav"

            # Resumable chunk synthesis: check if chunk already generated from prior attempt
            if chunk.index <= job.completed_chunk_index and chunk_file.exists() and chunk_file.stat().st_size > 0:
                logger.info(f"Skipping already completed chunk {chunk.index}/{len(chunks)}")
                generated_chunk_paths.append(chunk_file)
                continue

            # Synthesize chunk
            kokoro_client.synthesize_chunk(
                text=chunk.text,
                output_path=chunk_file,
                voice=settings.KOKORO_VOICE,
                speed=settings.KOKORO_SPEED,
            )

            job.completed_chunk_index = chunk.index
            db.commit()
            generated_chunk_paths.append(chunk_file)

        # Transition to ENCODING
        transition_job_state(db, job, JobState.ENCODING.value, component="herald-worker")

        # Construct final audio output filename
        now_str = datetime.now(UTC).strftime("%Y-%m-%d_%H%M")
        title_slug = slugify(title)
        short_id = job.id[:8]
        mp3_filename = f"{now_str}_{title_slug}_{short_id}.mp3"
        output_mp3_path = output_dir / mp3_filename

        # Assemble and normalize audio
        audio_info = join_and_normalize_audio(
            chunk_paths=generated_chunk_paths,
            output_mp3_path=output_mp3_path,
            episode_title=title,
            episode_description=description,
            job_id=job.id,
        )

        job.local_audio_path = str(output_mp3_path)
        job.audio_bytes = audio_info["file_bytes"]
        job.audio_duration_seconds = audio_info["duration_seconds"]
        job.audio_sha256 = audio_info["sha256"]
        db.commit()

        # Transition to AUDIO_READY
        transition_job_state(db, job, JobState.AUDIO_READY.value, component="herald-worker")
        logger.info(f"Successfully rendered audio for job '{job.id}': {output_mp3_path}")

        # Clean up intermediate chunk files
        try:
            shutil.rmtree(chunks_dir)
        except Exception as e:
            logger.warning(f"Failed to remove temporary chunk directory '{chunks_dir}': {e}")

        return True

    except Exception as e:
        logger.error(f"Error processing job '{job.id}': {e}", exc_info=True)
        job.attempt_count += 1
        db.commit()
        transition_job_state(
            db,
            job,
            JobState.FAILED.value,
            component="herald-worker",
            message=str(e),
            error_category="WORKER_PROCESSING_FAILURE",
        )
        return False


def run_worker_loop():
    """Main worker daemon loop."""
    logger.info("Starting Herald Worker daemon...")
    kokoro_client = KokoroClient()

    # Log health check status on startup
    h_status = kokoro_client.health_check()
    logger.info(f"Startup Kokoro/FFmpeg Health Status: {h_status}")

    poll_interval = 5  # seconds

    while True:
        try:
            db = SessionLocal()
            try:
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
