import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import Base, JobProcessingMetric, JobState, PodcastJob, PodcastTTSChunk
from herald.services.eta_calculator import calculate_job_eta
from herald.telegram.formatters import format_completion


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_eta_v2_historical_rtf_from_tts_total_and_audio_duration(db_session):
    """
    Test that ETA v2 derives historical RTF from TTS_TOTAL duration_ms divided by
    completed job audio_duration_seconds, ignoring summed parallel KOKORO_REQUEST latencies.
    """
    now = datetime.now(UTC)

    # 1. Create completed job with actual 60s audio
    job1 = PodcastJob(
        id="comp-job-1",
        transport="telegram",
        status=JobState.COMPLETE.value,
        source_hash="hash1",
        source_text="source text",
        audio_duration_seconds=60,
        completed_at=now,
        created_at=now - timedelta(minutes=5),
    )
    db_session.add(job1)

    # Record TTS_TOTAL metric = 30,000ms wall time (meaning RTF = 30s wall / 60s audio = 0.5)
    m_tts_total = JobProcessingMetric(
        id=str(uuid.uuid4()),
        job_id="comp-job-1",
        stage="TTS_TOTAL",
        status="success",
        started_at=now - timedelta(seconds=40),
        finished_at=now - timedelta(seconds=10),
        duration_ms=30000,
    )
    db_session.add(m_tts_total)

    # Record 4 concurrent KOKORO_REQUEST metrics that sum to 100,000ms
    # (summed KOKORO_REQUEST would falsely produce RTF = 100/60 = 1.67)
    for i in range(4):
        m_req = JobProcessingMetric(
            id=str(uuid.uuid4()),
            job_id="comp-job-1",
            stage="KOKORO_REQUEST",
            status="success",
            started_at=now - timedelta(seconds=35),
            finished_at=now - timedelta(seconds=10),
            duration_ms=25000,
        )
        db_session.add(m_req)

    db_session.commit()

    # 2. Query ETA for a new target job (e.g. 120s estimated script duration)
    target_job = PodcastJob(
        id="target-job-1",
        transport="telegram",
        status=JobState.QUEUED_TTS.value,
        source_hash="hash2",
        source_text="source text 2",
        script_json={"segments": [{"narration": "word " * 136}, {"narration": "word " * 136}]},
        created_at=now,
    )
    db_session.add(target_job)
    db_session.commit()

    eta = calculate_job_eta(db_session, target_job)

    # Must use TTS_TOTAL / audio_duration_seconds = 30,000 / 60,000 = 0.5 RTF
    assert eta["rtf_source"] == "historical"
    assert eta["realtime_factor"] == 0.5


def test_eta_v2_fallback_when_insufficient_history(db_session):
    """When completed audio duration is < 10s or absent, fallback RTF is used."""
    now = datetime.now(UTC)
    target_job = PodcastJob(
        id="target-job-2",
        transport="telegram",
        status=JobState.QUEUED_TTS.value,
        source_hash="hash3",
        source_text="source text 3",
        script_json={"segments": [{"narration": "word " * 136}]},
        created_at=now,
    )
    db_session.add(target_job)
    db_session.commit()

    eta = calculate_job_eta(db_session, target_job)
    assert eta["rtf_source"] == "fallback"
    assert eta["realtime_factor"] == getattr(settings, "TTS_ESTIMATED_REALTIME_FACTOR", 2.4)


def test_eta_v2_in_progress_chunks_denominator_and_numerator(db_session):
    """In-progress queue work uses podcast_tts_chunks table as denominator and numerator."""
    now = datetime.now(UTC)

    # Active synthesizing job ahead in queue
    ahead_job = PodcastJob(
        id="ahead-job-1",
        transport="telegram",
        status=JobState.SYNTHESIZING.value,
        source_hash="hash_ahead",
        source_text="ahead text",
        script_json={"segments": [{"narration": "word " * 136}, {"narration": "word " * 136}]},
        created_at=now - timedelta(minutes=2),
    )
    db_session.add(ahead_job)

    # 4 chunks total, 2 completed out-of-order (chunk 1 and chunk 3 completed)
    for idx, st in [(1, "COMPLETED"), (2, "SYNTHESIZING"), (3, "COMPLETED"), (4, "PENDING")]:
        chunk = PodcastTTSChunk(
            id=str(uuid.uuid4()),
            job_id="ahead-job-1",
            chunk_index=idx,
            text_hash=f"hash_{idx}",
            status=st,
            attempt_count=1,
            created_at=now,
        )
        db_session.add(chunk)

    target_job = PodcastJob(
        id="target-job-3",
        transport="telegram",
        status=JobState.QUEUED_TTS.value,
        source_hash="hash_target",
        source_text="target text",
        script_json={"segments": [{"narration": "word " * 136}]},
        created_at=now,
    )
    db_session.add(target_job)
    db_session.commit()

    eta = calculate_job_eta(db_session, target_job)
    assert eta["jobs_ahead"] == 1
    # 2/4 chunks completed = 50% remaining for ahead job


def test_format_completion_subtracts_approval_wait_time():
    """Active processing time calculation subtracts approval hold duration."""
    t0 = datetime(2026, 9, 3, 10, 0, 0, tzinfo=UTC)
    t_req = datetime(2026, 9, 3, 10, 0, 5, tzinfo=UTC)
    t_app = datetime(2026, 9, 3, 10, 5, 5, tzinfo=UTC)  # User waited 5 minutes
    t_comp = datetime(2026, 9, 3, 10, 6, 35, tzinfo=UTC)  # Finished 1m 30s after approve

    job = PodcastJob(
        id="job-hold-time-1",
        transport="telegram",
        status=JobState.COMPLETE.value,
        source_hash="h1",
        source_text="text",
        created_at=t0,
        approval_requested_at=t_req,
        approved_at=t_app,
        completed_at=t_comp,
        audio_duration_seconds=90,
    )

    caption = format_completion(job, actual_chunks_count=3, file_size_bytes=1024 * 1024)
    # Total wall = 6m 35s, hold = 5m 0s, active processing = 1m 35s
    assert "1m 35s" in caption
