from datetime import UTC, datetime, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.core.pipeline import find_prior_content_candidate
from herald.db.models import Base, JobState, PodcastJob, RequestMode, SourceType


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_no_candidates_returns_none(db_session):
    result = find_prior_content_candidate(db_session, source_hash="nonexistent")
    assert result is None


def test_active_tier_beats_all_others(db_session):
    now = datetime.now(UTC)
    h = "test-hash-1"

    # Failed job (most recent)
    failed = PodcastJob(
        id="job-failed",
        status=JobState.FAILED_FINAL.value,
        source_hash=h,
        source_text="sample text",
        created_at=now,
    )
    # Complete job (older)
    complete = PodcastJob(
        id="job-complete",
        status=JobState.COMPLETE.value,
        source_hash=h,
        source_text="sample text",
        created_at=now - timedelta(minutes=5),
    )
    # Active job (oldest)
    active = PodcastJob(
        id="job-active",
        status=JobState.QUEUED_TTS.value,
        source_hash=h,
        source_text="sample text",
        created_at=now - timedelta(minutes=10),
    )

    db_session.add_all([failed, complete, active])
    db_session.commit()

    candidate = find_prior_content_candidate(db_session, source_hash=h)
    assert candidate is not None
    assert candidate.id == "job-active"


def test_complete_tier_beats_cancelled_and_failed(db_session):
    now = datetime.now(UTC)
    h = "test-hash-2"

    cancelled = PodcastJob(
        id="job-cancelled",
        status=JobState.CANCELLED.value,
        source_hash=h,
        source_text="sample text",
        created_at=now,
    )
    complete = PodcastJob(
        id="job-complete",
        status=JobState.COMPLETE.value,
        source_hash=h,
        source_text="sample text",
        created_at=now - timedelta(minutes=5),
    )
    failed = PodcastJob(
        id="job-failed",
        status=JobState.FAILED_FINAL.value,
        source_hash=h,
        source_text="sample text",
        created_at=now,
    )

    db_session.add_all([cancelled, complete, failed])
    db_session.commit()

    candidate = find_prior_content_candidate(db_session, source_hash=h)
    assert candidate is not None
    assert candidate.id == "job-complete"


def test_cancelled_tier_beats_failed(db_session):
    now = datetime.now(UTC)
    h = "test-hash-3"

    failed = PodcastJob(
        id="job-failed",
        status=JobState.FAILED_FINAL.value,
        source_hash=h,
        source_text="sample text",
        created_at=now,
    )
    cancelled = PodcastJob(
        id="job-cancelled",
        status=JobState.CANCELLED.value,
        source_hash=h,
        source_text="sample text",
        created_at=now - timedelta(minutes=5),
    )

    db_session.add_all([failed, cancelled])
    db_session.commit()

    candidate = find_prior_content_candidate(db_session, source_hash=h)
    assert candidate is not None
    assert candidate.id == "job-cancelled"


def test_tie_breaking_by_created_at_desc(db_session):
    now = datetime.now(UTC)
    h = "test-hash-4"

    older_complete = PodcastJob(
        id="job-complete-older",
        status=JobState.COMPLETE.value,
        source_hash=h,
        source_text="sample text",
        created_at=now - timedelta(minutes=10),
    )
    newer_complete = PodcastJob(
        id="job-complete-newer",
        status=JobState.COMPLETE.value,
        source_hash=h,
        source_text="sample text",
        created_at=now - timedelta(minutes=2),
    )

    db_session.add_all([older_complete, newer_complete])
    db_session.commit()

    candidate = find_prior_content_candidate(db_session, source_hash=h)
    assert candidate is not None
    assert candidate.id == "job-complete-newer"


def test_exclude_job_id(db_session):
    now = datetime.now(UTC)
    h = "test-hash-5"

    complete = PodcastJob(
        id="job-complete-1",
        status=JobState.COMPLETE.value,
        source_hash=h,
        source_text="sample text",
        created_at=now,
    )
    db_session.add(complete)
    db_session.commit()

    # When excluding itself, should return None
    candidate = find_prior_content_candidate(db_session, source_hash=h, exclude_job_id="job-complete-1")
    assert candidate is None
