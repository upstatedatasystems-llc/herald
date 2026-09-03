import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import or_
from sqlalchemy.orm import Session

from herald.audio.ffmpeg_builder import (
    check_free_disk_mb,
    join_and_normalize_audio,
)
from herald.concurrency import get_semaphores, initialize_semaphores
from herald.config import settings
from herald.db.connection import SessionLocal
from herald.db.models import JobState, PodcastJob
from herald.db.state_machine import transition_job_state
from herald.services.performance_metrics import record_stage_metric
from herald.services.resource_monitor import TTSResourceMonitor
from herald.tts.chunk_manager import process_tts_chunks_parallel
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


def renew_worker_lease(db: Session, job_id: str, worker_id: str, lease_seconds: int = 300) -> bool:
    """
    Atomically extend heartbeat and lease_expires_at for an active worker job.
    Only renews if claimed_by and claim_owner still match worker_id.
    """
    job = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.id == job_id,
            PodcastJob.claimed_by == worker_id,
            PodcastJob.claim_owner == worker_id,
            PodcastJob.status.in_([JobState.SYNTHESIZING.value, JobState.ENCODING.value]),
        )
        .first()
    )
    if job:
        now = datetime.now(UTC)
        job.heartbeat_at = now
        job.last_heartbeat_at = now
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        db.commit()
        return True
    return False


class WorkerLeaseHeartbeat:
    """
    Lightweight background thread that periodically renews the worker lease and updates
    heartbeat fields in a separate SQLAlchemy session during SYNTHESIZING + ENCODING stages.
    """

    def __init__(self, job_id: str, worker_id: str, lease_seconds: int = 300, interval_seconds: int = 30):
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-hb-{self.job_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self):
        while not self._stop_event.wait(timeout=self.interval_seconds):
            try:
                db = SessionLocal()
                try:
                    if db.bind and db.bind.dialect.name == "sqlite":
                        break
                    success = renew_worker_lease(db, self.job_id, self.worker_id, self.lease_seconds)
                    if not success:
                        break
                finally:
                    db.close()
            except Exception as e:
                logger.debug(f"Heartbeat renewal error for job '{self.job_id}': {e}")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


def should_send_delivery_nudge(job: PodcastJob) -> bool:
    """Return True if an n8n post-commit delivery nudge should be sent for this job."""
    if getattr(job, "transport", "email") == "telegram":
        return False
    return bool(getattr(settings, "ENABLE_EVENT_DRIVEN_DELIVERY", True))


def send_delivery_nudge(job: PodcastJob) -> bool:
    """
    Send post-commit delivery nudge webhook (e.g. for legacy n8n delivery).
    Returns True if webhook request succeeded, False otherwise (always non-fatal).
    """
    if not should_send_delivery_nudge(job):
        return False

    nudge_url = getattr(settings, "DELIVERY_NUDGE_WEBHOOK_URL", "http://n8n:5678/webhook/herald-audio-ready")
    nudge_secret = getattr(settings, "DELIVERY_NUDGE_SECRET", "") or settings.HERALD_API_KEY
    nudge_timeout = getattr(settings, "DELIVERY_NUDGE_TIMEOUT_SECONDS", 3.0)
    try:
        import httpx

        logger.info(f"Sending post-commit delivery nudge for job '{job.id}' to {nudge_url}")
        headers = {"Content-Type": "application/json"}
        if nudge_secret:
            headers["X-API-Key"] = nudge_secret
            headers["X-Herald-Delivery-Token"] = nudge_secret
        with httpx.Client(timeout=nudge_timeout) as client:
            resp = client.post(nudge_url, json={"job_id": job.id, "event": "AUDIO_READY"}, headers=headers)
            return resp.status_code < 400
    except Exception as ne:
        logger.warning(f"Delivery nudge for job '{job.id}' failed non-fatally ({ne}).")
        return False


def recover_stale_claims(db: Session, stale_minutes: int = 15):
    """
    Detect jobs stuck in SYNTHESIZING or ENCODING whose lease and heartbeat have expired,
    and atomically requeue them back to QUEUED_TTS for retry.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=stale_minutes)

    stale_jobs = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.status.in_([JobState.SYNTHESIZING.value, JobState.ENCODING.value]),
        )
        .with_for_update(skip_locked=True)
        .all()
    )

    for job in stale_jobs:
        lease_exp = job.lease_expires_at
        if lease_exp and lease_exp.tzinfo is None:
            lease_exp = lease_exp.replace(tzinfo=UTC)

        last_active = job.heartbeat_at or job.last_heartbeat_at or job.claimed_at
        if last_active and last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=UTC)

        if lease_exp is not None:
            is_expired = (lease_exp < now) and (last_active is None or last_active < cutoff)
        else:
            is_expired = last_active is not None and last_active < cutoff

        if is_expired:
            logger.warning(f"Recovering stale worker claim for job '{job.id}' (last active: {last_active}, lease: {lease_exp})")
            pre_recovery_status = job.status
            job.claimed_at = None
            job.claim_owner = None
            job.claimed_by = None
            job.lease_expires_at = None
            job.last_heartbeat_at = None
            job.heartbeat_at = None

            target_state = (
                JobState.FAILED_FINAL.value if (job.synthesis_attempt_count or 0) >= 3 else JobState.QUEUED_TTS.value
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

            record_stage_metric(
                job_id=job.id,
                stage="LEASE_RECOVERY",
                started_at=last_active or now,
                finished_at=now,
                status="recovered",
                metadata_json={"recovered_from": pre_recovery_status, "attempts": job.synthesis_attempt_count},
                is_attempt_metric=True,
            )


def requeue_due_tts_retries(db: Session):
    """
    Atomically find due FAILED_RETRYABLE jobs whose failed_stage is a worker/TTS stage
    (QUEUED_TTS, SYNTHESIZING, ENCODING) and requeue them to QUEUED_TTS.
    """
    now = datetime.now(UTC)
    due_retry_jobs = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.status == JobState.FAILED_RETRYABLE.value,
            PodcastJob.failed_stage.in_(["QUEUED_TTS", JobState.SYNTHESIZING.value, JobState.ENCODING.value]),
            or_(PodcastJob.next_retry_at.is_(None), PodcastJob.next_retry_at <= now),
        )
        .order_by(PodcastJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .all()
    )

    for job in due_retry_jobs:
        job.next_retry_at = None
        transition_job_state(
            db,
            job,
            JobState.QUEUED_TTS.value,
            component="herald-worker-requeue",
            message="Requeued due TTS job for retry",
            force=True,
            commit=False,
        )
        db.commit()


def claim_next_job(db: Session, worker_id: str = "herald-worker", lease_seconds: int = 300) -> PodcastJob | None:
    """
    Claim 1 pending QUEUED_TTS job atomically using SELECT ... FOR UPDATE SKIP LOCKED.
    """
    requeue_due_tts_retries(db)

    now = datetime.now(UTC)
    job = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.status == JobState.QUEUED_TTS.value,
        )
        .order_by(PodcastJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )

    if not job:
        return None

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
        return None

    job.claimed_at = now
    job.claim_owner = worker_id
    job.claimed_by = worker_id
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.last_heartbeat_at = now
    job.heartbeat_at = now

    transition_job_state(
        db,
        job,
        JobState.SYNTHESIZING.value,
        component="herald-worker",
        message=f"Claimed worker job atomically by '{worker_id}' for TTS synthesis",
        commit=False,
    )
    db.commit()
    db.refresh(job)
    return job


def process_next_job(db: Session, kokoro_client: KokoroClient, worker_id: str = "herald-worker") -> bool:
    """
    Acquire 1 QUEUED_TTS job atomically, synthesize TTS chunks concurrently,
    assemble with FFmpeg, and transition status to AUDIO_READY.
    """
    job = claim_next_job(db, worker_id=worker_id)
    if not job:
        return False

    now = datetime.now(UTC)

    # Record TTS_QUEUE_WAIT metric
    try:
        from herald.db.models import JobStateTransition

        q_trans = (
            db.query(JobStateTransition)
            .filter(JobStateTransition.job_id == job.id, JobStateTransition.to_state == JobState.QUEUED_TTS.value)
            .order_by(JobStateTransition.created_at.desc())
            .first()
        )
        queue_start = q_trans.created_at if (q_trans and q_trans.created_at) else (job.updated_at or now)
        if queue_start and queue_start.tzinfo is None:
            queue_start = queue_start.replace(tzinfo=UTC)
        wait_ms = max(0, int((now - queue_start).total_seconds() * 1000))
        record_stage_metric(
            job_id=job.id,
            stage="TTS_QUEUE_WAIT",
            started_at=queue_start,
            finished_at=now,
            duration_ms=wait_ms,
            status="success",
            metadata_json={"worker_id": worker_id},
        )
    except Exception as me:
        logger.warning(f"Could not record TTS_QUEUE_WAIT metric for job '{job.id}': {me}")

    work_dir = Path(settings.HERALD_WORK_DIR)
    job_dir = work_dir / "jobs" / job.id
    chunks_dir = job_dir / "chunks"
    output_dir = work_dir / "output"

    chunks_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    t_tts_total_start = datetime.now(UTC)
    concurrency_config = settings.get_concurrency_config()
    semaphores = get_semaphores()

    with WorkerLeaseHeartbeat(job.id, worker_id, lease_seconds=300, interval_seconds=30):
        try:
            # Check free disk space before synthesis begins
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

            t_chunking_start = datetime.now(UTC)
            target_chunk_chars = job.tts_chunk_chars or getattr(settings, "TTS_CHUNK_DEFAULT_CHARS", 500)
            chunks = chunk_podcast_script(segments, max_chars=target_chunk_chars)
            t_chunking_finish = datetime.now(UTC)

            record_stage_metric(
                job_id=job.id,
                stage="TTS_CHUNKING",
                started_at=t_chunking_start,
                finished_at=t_chunking_finish,
                status="success",
                input_chars=sum(len(s.get("narration", "")) for s in segments),
                metadata_json={
                    "chunk_count": len(chunks),
                    "configured_chunk_chars": target_chunk_chars,
                },
            )
            logger.info(
                f"Script split into {len(chunks)} TTS chunks for job '{job.id}' "
                f"(Voice: {voice}, Speed: {speed}, Timeout: {synthesis_timeout}s, "
                f"TTS slots global: {concurrency_config.tts_global_slots}, per_job: {concurrency_config.tts_per_job})"
            )

            is_section_end_list = [c.is_section_end for c in chunks]

            monitor = TTSResourceMonitor(interval_seconds=5.0)
            monitor.start()

            generated_chunk_paths = []
            try:
                generated_chunk_paths = process_tts_chunks_parallel(
                    session_factory=SessionLocal,
                    job_id=job.id,
                    script_chunks=chunks,
                    voice=voice,
                    speed=speed,
                    synthesis_timeout=synthesis_timeout,
                    chunks_dir=chunks_dir,
                    kokoro_client=kokoro_client,
                    global_semaphore=semaphores.global_tts,
                    per_job_semaphore=semaphores.create_per_job_tts_semaphore(),
                    max_workers=concurrency_config.tts_per_job,
                    worker_id=worker_id,
                )
            finally:
                resource_aggregates = monitor.stop()
                job.tts_resource_metrics_json = resource_aggregates
                try:
                    db.commit()
                    db.expire_all()
                    from herald.db.models import PodcastTTSChunk

                    total_completed = (
                        db.query(PodcastTTSChunk)
                        .filter(PodcastTTSChunk.job_id == job.id, PodcastTTSChunk.status == "COMPLETED")
                        .count()
                    )
                    if total_completed > 0:
                        job.completed_chunk_index = total_completed
                except Exception:
                    if generated_chunk_paths:
                        job.completed_chunk_index = len(generated_chunk_paths)
                db.commit()

            t_tts_total_finish = datetime.now(UTC)
            record_stage_metric(
                job_id=job.id,
                stage="TTS_TOTAL",
                started_at=t_tts_total_start,
                finished_at=t_tts_total_finish,
                status="success",
                metadata_json={"chunks_count": len(chunks), "worker_id": worker_id},
            )

            # Transition to ENCODING
            transition_job_state(db, job, JobState.ENCODING.value, component="herald-worker")
            db.commit()

            # Output filename generation
            slug = slugify(title)
            output_mp3_path = output_dir / f"{job.id}_{slug}.mp3"

            t_ffmpeg_start = datetime.now(UTC)
            audio_info = None

            # Execute FFmpeg join (concurrency protected internally within join_and_normalize_audio)
            for ffmpeg_attempt in range(1, 3):
                try:
                    logger.info(f"Worker '{worker_id}' assembling audio with FFmpeg for job '{job.id}' (Attempt {ffmpeg_attempt})...")
                    audio_info = join_and_normalize_audio(
                        chunk_paths=generated_chunk_paths,
                        output_mp3_path=output_mp3_path,
                        is_section_end_list=is_section_end_list,
                        episode_title=title,
                        episode_description=description,
                        job_id=job.id,
                    )

                    record_stage_metric(
                        job_id=job.id,
                        stage="FFMPEG_ENCODING",
                        started_at=t_ffmpeg_start,
                        finished_at=datetime.now(UTC),
                        status="success",
                        output_bytes=audio_info["file_bytes"],
                        audio_duration_ms=audio_info["duration_seconds"] * 1000 if audio_info.get("duration_seconds") else None,
                        attempt=ffmpeg_attempt,
                        metadata_json={"worker_id": worker_id},
                    )
                    break
                except Exception as fe:
                    logger.warning(f"FFmpeg assembly attempt {ffmpeg_attempt} failed: {fe}")
                    if output_mp3_path.exists():
                        output_mp3_path.unlink(missing_ok=True)
                    if ffmpeg_attempt == 2:
                        record_stage_metric(
                            job_id=job.id,
                            stage="FFMPEG_ENCODING",
                            started_at=t_ffmpeg_start,
                            finished_at=datetime.now(UTC),
                            status="failed",
                            attempt=ffmpeg_attempt,
                            metadata_json={"error": str(fe), "worker_id": worker_id},
                        )
                        raise

            job.completed_chunk_index = len(chunks)
            job.local_audio_path = audio_info["output_path"]
            job.audio_bytes = audio_info["file_bytes"]
            job.audio_duration_seconds = audio_info["duration_seconds"]
            job.audio_sha256 = audio_info["sha256"]
            job.audio_ready_at = datetime.now(UTC)
            job.kokoro_voice = voice
            job.kokoro_speed = speed
            job.gemini_model = settings.GEMINI_MODEL

            # Clear claim fields on successful completion
            job.claimed_at = None
            job.claim_owner = None
            job.claimed_by = None
            job.lease_expires_at = None
            job.last_heartbeat_at = None
            job.heartbeat_at = None
            db.commit()

            # Generate companion details artifact
            try:
                from herald.audio.artifact_generator import ensure_details_artifact

                ensure_details_artifact(job, output_dir)
            except Exception as se:
                logger.warning(f"Artifact creation warning for job '{job.id}': {se}")

            transition_job_state(db, job, JobState.AUDIO_READY.value, component="herald-worker")
            logger.info(f"Worker '{worker_id}' successfully rendered audio for job '{job.id}': {output_mp3_path}")

            # Post-commit delivery nudge (only for non-Telegram jobs, e.g. legacy email/n8n)
            send_delivery_nudge(job)

            try:
                shutil.rmtree(chunks_dir)
            except Exception as e:
                logger.warning(f"Failed to remove temporary chunk directory '{chunks_dir}': {e}")

            return True

        except Exception as e:
            logger.error(f"Error in worker '{worker_id}' processing job '{job.id}': {e}")
            try:
                db.rollback()
            except Exception:
                pass

            job = db.query(PodcastJob).filter(PodcastJob.id == job.id).first() or job
            job.attempt_count = (job.attempt_count or 0) + 1

            try:
                from herald.db.models import PodcastTTSChunk

                completed_cnt = (
                    db.query(PodcastTTSChunk)
                    .filter(PodcastTTSChunk.job_id == job.id, PodcastTTSChunk.status == "COMPLETED")
                    .count()
                )
                if completed_cnt > 0:
                    job.completed_chunk_index = completed_cnt
            except Exception:
                pass

            is_timeout = isinstance(e, KokoroTTSTimeoutError) or "timed out" in str(e).lower()
            err_code = "KOKORO_SYNTHESIS_TIMEOUT" if is_timeout else "KOKORO_SYNTHESIS_FAILED"
            job.error_code = err_code
            job.error_detail = str(e)
            job.failed_stage = JobState.SYNTHESIZING.value

            # Calculate bounded exponential retry backoff (15s, 30s, 60s...)
            attempts = job.synthesis_attempt_count or 1
            backoff_sec = min(300, 15 * (2 ** (attempts - 1)))
            job.next_retry_at = datetime.now(UTC) + timedelta(seconds=backoff_sec)

            # Clear claim/lease fields on failure
            job.claimed_at = None
            job.claim_owner = None
            job.claimed_by = None
            job.lease_expires_at = None
            job.last_heartbeat_at = None
            job.heartbeat_at = None

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
            db.commit()
            return False


def run_single_worker_loop(worker_id: str = "herald-worker"):
    """Single worker thread processing loop."""
    logger.info(f"Worker loop thread '{worker_id}' started.")
    kokoro_client = KokoroClient()
    poll_interval = 5

    while True:
        try:
            db = SessionLocal()
            try:
                recover_stale_claims(db)
                job_processed = process_next_job(db, kokoro_client, worker_id=worker_id)
            finally:
                db.close()

            if not job_processed:
                time.sleep(poll_interval)
        except Exception as e:
            logger.error(f"Unexpected error in worker loop '{worker_id}': {e}")
            time.sleep(poll_interval)


def run_worker_loop():
    """Main worker daemon startup & thread pool manager."""
    logger.info("Starting Herald Worker daemon...")
    concurrency_config = settings.get_concurrency_config()
    concurrency_config.log_diagnostics()

    # Initialize process-global semaphores once
    initialize_semaphores(concurrency_config)

    kokoro_client = KokoroClient()
    h_status = kokoro_client.health_check()
    logger.info(f"Startup Kokoro/FFmpeg Health Status: {h_status}")

    w_count = concurrency_config.worker_concurrency

    if w_count <= 1:
        run_single_worker_loop("herald-worker")
    else:
        logger.info(f"Launching {w_count} parallel episode worker threads...")
        with ThreadPoolExecutor(max_workers=w_count, thread_name_prefix="herald-worker") as executor:
            futures = [
                executor.submit(run_single_worker_loop, f"herald-worker-{i+1}")
                for i in range(w_count)
            ]
            for fut in futures:
                fut.result()


if __name__ == "__main__":
    run_worker_loop()
