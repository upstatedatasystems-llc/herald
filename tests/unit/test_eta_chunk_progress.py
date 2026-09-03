from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.db.models import Base, JobState, PodcastJob, PodcastTTSChunk
from herald.services.eta_calculator import calculate_job_eta


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


def test_target_job_eta_monotonically_decreases_with_chunk_progress(db_session):
    """
    Test that calculate_job_eta adjusts target job's own remaining synthesis time
    based on actual COMPLETED chunk records:
    - 0/12 chunks complete -> full synthesis ETA
    - 6/12 chunks complete -> ~50% synthesis ETA
    - 11/12 chunks complete -> ~1/12 synthesis ETA
    - 12/12 chunks / ENCODING -> minimal overhead ETA
    - Out-of-order chunk completion indices verify status == 'COMPLETED' count is queried.
    """
    now = datetime.now(UTC)
    segments = [
        {"speaker": "Host", "narration": f"Segment {i} with approximately ten words of narration."}
        for i in range(12)
    ]
    job = PodcastJob(
        id="eta-progress-job-1111-2222-333333333333",
        transport="telegram",
        status=JobState.SYNTHESIZING.value,
        source_hash="h_eta_p",
        source_text="Sample text",
        script_json={"episode_title": "12 Chunk Episode", "segments": segments},
        created_at=now,
    )
    db_session.add(job)

    # Insert 12 chunk records (all PENDING initially)
    chunks = []
    for i in range(1, 13):
        c = PodcastTTSChunk(
            job_id=job.id,
            chunk_index=i,
            text_hash=f"th_{i}",
            status="PENDING",
            created_at=now,
        )
        chunks.append(c)
        db_session.add(c)
    db_session.commit()

    # 1. 0/12 completed
    eta_0 = calculate_job_eta(db_session, job)
    t_rem_0 = eta_0["estimated_remaining_processing_seconds"]

    # 2. 6/12 completed (mark out-of-order: 2, 4, 6, 8, 10, 12 as COMPLETED)
    for idx in (2, 4, 6, 8, 10, 12):
        chunks[idx - 1].status = "COMPLETED"
    db_session.commit()

    eta_6 = calculate_job_eta(db_session, job)
    t_rem_6 = eta_6["estimated_remaining_processing_seconds"]
    assert t_rem_6 < t_rem_0

    # 3. 11/12 completed (mark 1, 3, 5, 7, 9 also COMPLETED; only chunk 11 remains PENDING)
    for idx in (1, 3, 5, 7, 9):
        chunks[idx - 1].status = "COMPLETED"
    db_session.commit()

    eta_11 = calculate_job_eta(db_session, job)
    t_rem_11 = eta_11["estimated_remaining_processing_seconds"]
    assert t_rem_11 < t_rem_6

    # 4. 12/12 completed & job transitions to ENCODING
    chunks[10].status = "COMPLETED"
    job.status = JobState.ENCODING.value
    db_session.commit()

    eta_enc = calculate_job_eta(db_session, job)
    t_rem_enc = eta_enc["estimated_remaining_processing_seconds"]
    assert t_rem_enc < t_rem_11

    # Verify strictly monotonic decreasing sequence
    assert t_rem_0 > t_rem_6 > t_rem_11 > t_rem_enc
