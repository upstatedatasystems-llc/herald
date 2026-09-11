from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import Base, JobState, PodcastJob
from herald.services.eta_calculator import calculate_job_eta


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_eta_fallback_when_no_first_chunk(db_session):
    now = datetime.now(UTC)
    job = PodcastJob(
        id="job-eta-1",
        source_hash="hash-eta-1",
        source_text="Some text",
        status=JobState.QUEUED_TTS.value,
        script_json={"segments": [{"narration": "Hello world"}]},
        created_at=now,
    )
    db_session.add(job)
    db_session.commit()

    eta = calculate_job_eta(db_session, job)
    assert eta["rtf_source"] == "fallback"
    assert eta["realtime_factor"] == getattr(settings, "TTS_ESTIMATED_REALTIME_FACTOR", 2.4)


def test_eta_blended_with_first_chunk_rtf(db_session):
    now = datetime.now(UTC)
    job = PodcastJob(
        id="job-eta-2",
        source_hash="hash-eta-2",
        source_text="Some text",
        status=JobState.SYNTHESIZING.value,
        script_json={"segments": [{"narration": "Hello world"}]},
        created_at=now,
    )
    db_session.add(job)
    db_session.commit()

    # Pass measured_first_chunk_rtf = 1.0
    # Expected blended = 0.5 * 1.0 + 0.5 * 2.4 = 1.7
    eta = calculate_job_eta(db_session, job, measured_first_chunk_rtf=1.0)
    assert "first_chunk_blended" in eta["rtf_source"]
    assert eta["realtime_factor"] == 1.7


def test_eta_high_outlier_clamped(db_session):
    now = datetime.now(UTC)
    job = PodcastJob(
        id="job-eta-3",
        source_hash="hash-eta-3",
        source_text="Some text",
        status=JobState.SYNTHESIZING.value,
        script_json={"segments": [{"narration": "Hello world"}]},
        created_at=now,
    )
    db_session.add(job)
    db_session.commit()

    # Measured RTF 30.0 should be clamped to 10.0
    # Expected blended = 0.5 * 10.0 + 0.5 * 2.4 = 6.2
    eta = calculate_job_eta(db_session, job, measured_first_chunk_rtf=30.0)
    assert eta["realtime_factor"] == 6.2


def test_eta_low_outlier_clamped(db_session):
    now = datetime.now(UTC)
    job = PodcastJob(
        id="job-eta-4",
        source_hash="hash-eta-4",
        source_text="Some text",
        status=JobState.SYNTHESIZING.value,
        script_json={"segments": [{"narration": "Hello world"}]},
        created_at=now,
    )
    db_session.add(job)
    db_session.commit()

    # Measured RTF 0.05 should be clamped to 0.5
    # Expected blended = 0.5 * 0.5 + 0.5 * 2.4 = 1.45
    eta = calculate_job_eta(db_session, job, measured_first_chunk_rtf=0.05)
    assert eta["realtime_factor"] == 1.45
