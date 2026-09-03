import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.worker.main import claim_next_job, recover_stale_claims
from herald.audio.ffmpeg_builder import generate_silence_wav
from herald.concurrency import ConcurrencyConfig, get_semaphores
from herald.db.connection import SessionLocal
from herald.db.models import JobState, PodcastJob, PodcastTTSChunk
from herald.tts.chunk_manager import (
    process_tts_chunks_parallel,
    sync_and_prepare_chunks,
)
from herald.tts.chunker import TTSChunk


@pytest.fixture
def test_job(db_session: Session) -> PodcastJob:
    job = PodcastJob(
        id=str(uuid.uuid4()),
        gmail_message_id=f"msg_{uuid.uuid4()}",
        sender_email="test@example.com",
        source_hash="hash123",
        source_text="Test source text for concurrency",
        request_mode="standard",
        status=JobState.QUEUED_TTS.value,
        script_json={
            "episode_title": "Test Concurrency Episode",
            "episode_description": "Description",
            "segments": [
                {"speaker": "Host", "narration": "Welcome to the show segment 1."},
                {"speaker": "Co-Host", "narration": "Thanks for having me segment 2."},
            ],
        },
        created_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def test_atomic_worker_claiming_skip_locked(db_session: Session, test_job: PodcastJob):
    # Test atomic claiming behavior
    claimed_1 = claim_next_job(db_session, worker_id="worker-1")
    assert claimed_1 is not None
    assert claimed_1.id == test_job.id
    assert claimed_1.claimed_by == "worker-1"
    assert claimed_1.status == JobState.SYNTHESIZING.value

    # Second worker attempting to claim when job is already claimed/locked
    claimed_2 = claim_next_job(db_session, worker_id="worker-2")
    assert claimed_2 is None



def test_recover_stale_claims(db_session: Session, test_job: PodcastJob):
    # Simulate worker crash: job stuck in SYNTHESIZING with expired lease
    past_time = datetime.now(UTC) - timedelta(minutes=20)
    test_job.status = JobState.SYNTHESIZING.value
    test_job.claimed_by = "crashed-worker"
    test_job.claimed_at = past_time
    test_job.lease_expires_at = past_time
    test_job.last_heartbeat_at = past_time
    test_job.synthesis_attempt_count = 1
    db_session.commit()

    recover_stale_claims(db_session, stale_minutes=15)
    db_session.refresh(test_job)

    assert test_job.claimed_by is None
    assert test_job.status == JobState.QUEUED_TTS.value


def test_durable_chunk_tracking_and_resume(db_session: Session, test_job: PodcastJob, tmp_path: Path):
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()

    script_chunks = [
        TTSChunk(index=0, text="First chunk text", is_section_end=False),
        TTSChunk(index=1, text="Second chunk text", is_section_end=True),
    ]

    prepared = sync_and_prepare_chunks(
        db_session, test_job.id, script_chunks, "af_heart", 1.0, chunks_dir
    )
    assert len(prepared) == 2
    assert all(c.status == "PENDING" for c in prepared)

    # Simulate chunk 0 completing
    c0_file = chunks_dir / "chunk_0000.wav"
    generate_silence_wav(c0_file, duration_seconds=0.5)

    db_c0 = db_session.query(PodcastTTSChunk).filter_by(job_id=test_job.id, chunk_index=0).first()
    db_c0.status = "COMPLETED"
    db_c0.local_path = str(c0_file)
    db_session.commit()

    # Re-run prepare - chunk 0 should be reused, chunk 1 remains PENDING
    re_prepared = sync_and_prepare_chunks(
        db_session, test_job.id, script_chunks, "af_heart", 1.0, chunks_dir
    )
    assert len(re_prepared) == 2
    assert re_prepared[0].status == "COMPLETED"
    assert re_prepared[1].status == "PENDING"


def test_chunk_hash_invalidation(db_session: Session, test_job: PodcastJob, tmp_path: Path):
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()

    initial_chunks = [TTSChunk(index=0, text="Original text", is_section_end=False)]
    sync_and_prepare_chunks(db_session, test_job.id, initial_chunks, "af_heart", 1.0, chunks_dir)

    c0_file = chunks_dir / "chunk_0000.wav"
    generate_silence_wav(c0_file, duration_seconds=0.5)
    db_c0 = db_session.query(PodcastTTSChunk).filter_by(job_id=test_job.id, chunk_index=0).first()
    db_c0.status = "COMPLETED"
    db_c0.local_path = str(c0_file)
    db_session.commit()

    # Change text of chunk 0
    modified_chunks = [TTSChunk(index=0, text="Modified text text", is_section_end=False)]
    res = sync_and_prepare_chunks(db_session, test_job.id, modified_chunks, "af_heart", 1.0, chunks_dir)

    # Chunk should be invalidated back to PENDING because text_hash changed
    assert res[0].status == "PENDING"
    assert not c0_file.exists()


class MockKokoroClient:
    def synthesize_chunk(self, text: str, output_path: Path, voice: str, speed: float, timeout: float):
        generate_silence_wav(output_path, duration_seconds=0.2)


def test_parallel_chunk_processing_out_of_order_and_ordered_assembly(
    db_session: Session, test_job: PodcastJob, tmp_path: Path
):
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()

    script_chunks = [
        TTSChunk(index=0, text="Chunk zero narration text.", is_section_end=False),
        TTSChunk(index=1, text="Chunk one narration text.", is_section_end=False),
        TTSChunk(index=2, text="Chunk two narration text.", is_section_end=True),
    ]

    mock_kokoro = MockKokoroClient()
    config = ConcurrencyConfig("auto", 4, 2, 2, 4, 2, 1, 2)
    semaphores = get_semaphores(config)

    ordered_paths = process_tts_chunks_parallel(
        session_factory=SessionLocal,
        job_id=test_job.id,
        script_chunks=script_chunks,
        voice="af_heart",
        speed=1.0,
        synthesis_timeout=30.0,
        chunks_dir=chunks_dir,
        kokoro_client=mock_kokoro,
        global_semaphore=semaphores.global_tts,
        per_job_semaphore=semaphores.create_per_job_tts_semaphore(),
        max_workers=2,
        worker_id="test-worker",
    )

    assert len(ordered_paths) == 3
    assert ordered_paths[0].name == "chunk_0000.wav"
    assert ordered_paths[1].name == "chunk_0001.wav"
    assert ordered_paths[2].name == "chunk_0002.wav"
    assert all(p.exists() and p.stat().st_size > 0 for p in ordered_paths)
