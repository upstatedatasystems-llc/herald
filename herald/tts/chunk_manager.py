import hashlib
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from threading import Semaphore

from sqlalchemy.orm import Session

from herald.audio.ffmpeg_builder import validate_audio_file
from herald.concurrency import tts_slot_lock
from herald.db.models import PodcastJob, PodcastTTSChunk
from herald.services.performance_metrics import record_stage_metric
from herald.tts.chunker import TTSChunk
from herald.tts.kokoro_client import (
    KokoroClient,
    KokoroTTSError,
    KokoroTTSTimeoutError,
)

logger = logging.getLogger("herald.tts.chunk_manager")


def compute_chunk_text_hash(chunk_text: str, voice: str, speed: float) -> str:
    """Compute SHA-256 hash for chunk content and synthesis parameters."""
    raw = f"{voice.strip()}:{round(float(speed), 2)}:{chunk_text.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sync_and_prepare_chunks(
    db: Session,
    job_id: str,
    script_chunks: list[TTSChunk],
    voice: str,
    speed: float,
    chunks_dir: Path,
) -> list[PodcastTTSChunk]:
    """
    Synchronize DB records in podcast_tts_chunks with the script chunks.
    Validates completed chunk files and invalidates stale chunk records/files.
    """
    existing_chunks = (
        db.query(PodcastTTSChunk)
        .filter(PodcastTTSChunk.job_id == job_id)
        .order_by(PodcastTTSChunk.chunk_index.asc())
        .all()
    )
    existing_by_index = {c.chunk_index: c for c in existing_chunks}

    prepared: list[PodcastTTSChunk] = []

    for chunk in script_chunks:
        text_hash = compute_chunk_text_hash(chunk.text, voice, speed)
        chunk_file = chunks_dir / f"chunk_{chunk.index:04d}.wav"

        db_chunk = existing_by_index.get(chunk.index)

        if db_chunk:
            # Check if text hash matches
            if db_chunk.text_hash == text_hash:
                if db_chunk.status == "COMPLETED" and chunk_file.exists() and chunk_file.stat().st_size > 0:
                    try:
                        validate_audio_file(chunk_file)
                        logger.info(f"Reusing existing valid chunk {chunk.index} for job '{job_id}'")
                        prepared.append(db_chunk)
                        continue
                    except Exception as ve:
                        logger.warning(f"Existing chunk file for index {chunk.index} failed validation ({ve}). Resetting...")
                
                # Reset to PENDING if not valid completed
                db_chunk.status = "PENDING"
                db_chunk.local_path = str(chunk_file)
                db_chunk.error_detail = None
            else:
                # Text hash changed - invalidate chunk audio and reset record
                logger.info(f"Chunk {chunk.index} text hash changed for job '{job_id}'. Invalidating cached chunk.")
                if chunk_file.exists():
                    chunk_file.unlink(missing_ok=True)
                db_chunk.text_hash = text_hash
                db_chunk.status = "PENDING"
                db_chunk.attempt_count = 0
                db_chunk.local_path = str(chunk_file)
                db_chunk.audio_duration = None
                db_chunk.completed_at = None
                db_chunk.error_detail = None
            prepared.append(db_chunk)
        else:
            # Check if this chunk was completed in a previous attempt or legacy run
            job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
            is_prev_completed = (
                job
                and job.completed_chunk_index is not None
                and chunk.index <= job.completed_chunk_index
                and chunk_file.exists()
                and chunk_file.stat().st_size > 0
            )
            if is_prev_completed:
                try:
                    validate_audio_file(chunk_file)
                    logger.info(f"Reusing existing valid chunk file {chunk.index} for job '{job_id}'")
                    new_chunk = PodcastTTSChunk(
                        job_id=job_id,
                        chunk_index=chunk.index,
                        text_hash=text_hash,
                        status="COMPLETED",
                        attempt_count=1,
                        local_path=str(chunk_file),
                        completed_at=datetime.now(UTC),
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                    db.add(new_chunk)
                    prepared.append(new_chunk)
                    continue
                except Exception as ve:
                    logger.warning(f"Existing chunk file for index {chunk.index} failed validation ({ve})")

            # New pending chunk record
            new_chunk = PodcastTTSChunk(
                job_id=job_id,
                chunk_index=chunk.index,
                text_hash=text_hash,
                status="PENDING",
                attempt_count=0,
                local_path=str(chunk_file),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            db.add(new_chunk)
            prepared.append(new_chunk)


    # Clean up obsolete chunk records past current script length
    for idx, old_chunk in existing_by_index.items():
        if idx > len(script_chunks):
            if old_chunk.local_path:
                Path(old_chunk.local_path).unlink(missing_ok=True)
            db.delete(old_chunk)

    db.commit()

    # Re-query all chunks ordered by index
    return (
        db.query(PodcastTTSChunk)
        .filter(PodcastTTSChunk.job_id == job_id)
        .order_by(PodcastTTSChunk.chunk_index.asc())
        .all()
    )


def synthesize_single_chunk(
    session_factory: Callable[[], Session],
    job_id: str,
    chunk: TTSChunk,
    voice: str,
    speed: float,
    synthesis_timeout: float,
    chunks_dir: Path,
    kokoro_client: KokoroClient,
    global_semaphore: Semaphore,
    per_job_semaphore: Semaphore,
    worker_id: str,
    total_chunks: int = 1,
) -> Path:
    """
    Synthesize a single chunk bounded by both global and per-job semaphores.
    Updates DB status and records performance metrics.
    """
    chunk_file = chunks_dir / f"chunk_{chunk.index:04d}.wav"

    # Acquire semaphores (job limit first, then global server-wide limit)
    with per_job_semaphore:
        with global_semaphore:
            db = session_factory()
            try:
                db_chunk = (
                    db.query(PodcastTTSChunk)
                    .filter(PodcastTTSChunk.job_id == job_id, PodcastTTSChunk.chunk_index == chunk.index)
                    .first()
                )
                if not db_chunk:
                    raise KokoroTTSError(f"TTS Chunk record not found for index {chunk.index}")

                db_chunk.status = "SYNTHESIZING"
                db_chunk.claimed_by = worker_id
                db_chunk.started_at = datetime.now(UTC)
                db_chunk.attempt_count += 1
                db.commit()

                max_attempts = 2
                chunk_success = False
                last_error: Exception | None = None

                for attempt in range(1, max_attempts + 1):
                    t0_utc = datetime.now(UTC)
                    t0_mono = time.monotonic()
                    try:
                        logger.info(
                            f"Worker '{worker_id}' synthesizing chunk {chunk.index}/{total_chunks} (attempt {attempt}): "
                            f"{len(chunk.text)} chars | timeout: {synthesis_timeout}s"
                        )
                        with tts_slot_lock(db=db):
                            kokoro_client.synthesize_chunk(
                                text=chunk.text,
                                output_path=chunk_file,
                                voice=voice,
                                speed=speed,
                                timeout=synthesis_timeout,
                            )
                        val_meta = validate_audio_file(chunk_file)

                        t1_mono = time.monotonic()
                        t1_utc = datetime.now(UTC)
                        elapsed_ms = max(0, int((t1_mono - t0_mono) * 1000))
                        output_bytes = val_meta.get("size_bytes", chunk_file.stat().st_size if chunk_file.exists() else 0)
                        audio_dur_sec = val_meta.get("duration_seconds")
                        audio_dur_ms = max(0, int(audio_dur_sec * 1000)) if (audio_dur_sec is not None and audio_dur_sec > 0) else None
                        rtf_val = round((elapsed_ms / float(audio_dur_ms)) if audio_dur_ms and audio_dur_ms > 0 else 0.0, 3)

                        record_stage_metric(
                            job_id=job_id,
                            stage="KOKORO_REQUEST",
                            sequence_index=chunk.index,
                            attempt=attempt,
                            started_at=t0_utc,
                            finished_at=t1_utc,
                            duration_ms=elapsed_ms,
                            status="success",
                            input_chars=len(chunk.text),
                            output_bytes=output_bytes,
                            audio_duration_ms=audio_dur_ms,
                            metadata_json={
                                "voice": voice,
                                "speed": speed,
                                "rtf": rtf_val,
                                "worker_id": worker_id,
                                "timeout_seconds": synthesis_timeout,
                            },
                            is_attempt_metric=True,
                        )

                        # Update DB record to COMPLETED
                        db_chunk.status = "COMPLETED"
                        db_chunk.local_path = str(chunk_file)
                        db_chunk.audio_duration = audio_dur_sec
                        db_chunk.completed_at = t1_utc
                        db_chunk.error_detail = None
                        db.commit()

                        # Update PodcastJob completed_chunk_index & heartbeat
                        job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
                        if job:
                            max_completed = (
                                db.query(PodcastTTSChunk)
                                .filter(PodcastTTSChunk.job_id == job_id, PodcastTTSChunk.status == "COMPLETED")
                                .count()
                            )
                            job.completed_chunk_index = max_completed
                            job.last_heartbeat_at = datetime.now(UTC)
                            job.heartbeat_at = datetime.now(UTC)
                            db.commit()
                            logger.info(f"Job '{job_id}' progress: {max_completed}/{total_chunks} chunks completed")

                        logger.info(f"Chunk {chunk.index}/{total_chunks} completed successfully in {elapsed_ms / 1000.0:.1f}s")
                        chunk_success = True
                        break

                    except Exception as ce:
                        t1_mono = time.monotonic()
                        t1_utc = datetime.now(UTC)
                        elapsed_ms = max(0, int((t1_mono - t0_mono) * 1000))
                        last_error = ce
                        if chunk_file.exists():
                            chunk_file.unlink(missing_ok=True)

                        is_timeout = isinstance(ce, KokoroTTSTimeoutError) or "timed out" in str(ce).lower()
                        record_stage_metric(
                            job_id=job_id,
                            stage="KOKORO_REQUEST",
                            sequence_index=chunk.index,
                            attempt=attempt,
                            started_at=t0_utc,
                            finished_at=t1_utc,
                            duration_ms=elapsed_ms,
                            status="failed",
                            input_chars=len(chunk.text),
                            metadata_json={
                                "voice": voice,
                                "speed": speed,
                                "worker_id": worker_id,
                                "timeout_indicator": is_timeout,
                                "error": str(ce),
                            },
                            is_attempt_metric=True,
                        )
                        logger.warning(f"Chunk {chunk.index} attempt {attempt} failed ({ce})")
                        if attempt < max_attempts:
                            time.sleep(1.0)

                if not chunk_success:
                    db_chunk.status = "FAILED"
                    db_chunk.error_detail = str(last_error)
                    db.commit()

                    is_timeout = isinstance(last_error, KokoroTTSTimeoutError) or "timed out" in str(last_error).lower()
                    if is_timeout:
                        if isinstance(last_error, KokoroTTSTimeoutError):
                            raise last_error
                        raise KokoroTTSTimeoutError(str(last_error))
                    if isinstance(last_error, KokoroTTSError):
                        raise last_error
                    raise KokoroTTSError(str(last_error))


                return chunk_file

            finally:
                db.close()


def process_tts_chunks_parallel(
    session_factory: Callable[[], Session],
    job_id: str,
    script_chunks: list[TTSChunk],
    voice: str,
    speed: float,
    synthesis_timeout: float,
    chunks_dir: Path,
    kokoro_client: KokoroClient,
    global_semaphore: Semaphore,
    per_job_semaphore: Semaphore,
    max_workers: int = 2,
    worker_id: str = "herald-worker",
) -> list[Path]:
    """
    Process all chunks for an episode in parallel bounded by semaphores.
    Returns list of chunk file Paths strictly ordered by chunk_index.
    """
    db = session_factory()
    try:
        if db.bind and db.bind.dialect.name == "sqlite":
            max_workers = 1
        prepared_chunks = sync_and_prepare_chunks(
            db, job_id, script_chunks, voice, speed, chunks_dir
        )
    finally:
        db.close()


    # Index map for TTSChunk objects
    chunk_map = {c.index: c for c in script_chunks}

    # Identify chunks needing synthesis
    uncompleted_chunks = [c for c in prepared_chunks if c.status != "COMPLETED"]

    if uncompleted_chunks:
        logger.info(f"Job '{job_id}' synthesis: {len(uncompleted_chunks)}/{len(script_chunks)} chunks pending (Workers limit: {max_workers})")

        executor_workers = min(max_workers, len(uncompleted_chunks))
        with ThreadPoolExecutor(max_workers=executor_workers, thread_name_prefix=f"tts-{job_id[:8]}") as executor:
            future_to_idx = {}
            for db_c in uncompleted_chunks:
                chunk_obj = chunk_map[db_c.chunk_index]
                fut = executor.submit(
                    synthesize_single_chunk,
                    session_factory=session_factory,
                    job_id=job_id,
                    chunk=chunk_obj,
                    voice=voice,
                    speed=speed,
                    synthesis_timeout=synthesis_timeout,
                    chunks_dir=chunks_dir,
                    kokoro_client=kokoro_client,
                    global_semaphore=global_semaphore,
                    per_job_semaphore=per_job_semaphore,
                    worker_id=worker_id,
                    total_chunks=len(script_chunks),
                )
                future_to_idx[fut] = db_c.chunk_index

            # Collect results and handle errors
            for fut in as_completed(future_to_idx):
                idx = future_to_idx[fut]
                try:
                    fut.result()
                except Exception as exc:
                    logger.error(f"TTS Chunk {idx} raised fatal error: {exc}")
                    # Cancel pending tasks where possible
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise exc

    # Final verification and ordering
    db = session_factory()
    try:
        final_chunks = (
            db.query(PodcastTTSChunk)
            .filter(PodcastTTSChunk.job_id == job_id)
            .order_by(PodcastTTSChunk.chunk_index.asc())
            .all()
        )

        ordered_paths: list[Path] = []
        for db_c in final_chunks:
            if db_c.status != "COMPLETED" or not db_c.local_path:
                raise KokoroTTSError(f"TTS Chunk {db_c.chunk_index} is not complete (status: {db_c.status})")
            file_path = Path(db_c.local_path)
            if not file_path.exists() or file_path.stat().st_size == 0:
                raise KokoroTTSError(f"TTS Chunk {db_c.chunk_index} file missing or empty: {file_path}")
            validate_audio_file(file_path)
            ordered_paths.append(file_path)

        return ordered_paths
    finally:
        db.close()
