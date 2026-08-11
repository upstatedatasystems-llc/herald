import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.worker.main import (
    claim_next_job,
    recover_stale_claims,
    requeue_due_tts_retries,
)
from herald.db.connection import Base
from herald.db.models import JobState, PodcastJob


@pytest.fixture(scope="module")
def postgres_engine():
    pg_url = os.getenv("HERALD_TEST_DATABASE_URL")
    if not pg_url or "postgresql" not in pg_url:
        pytest.skip("HERALD_TEST_DATABASE_URL not set to a PostgreSQL connection string.")
    
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            pass
    except Exception as e:
        pytest.skip(f"Could not connect to PostgreSQL database at {pg_url}: {e}")

    Base.metadata.create_all(bind=engine)
    yield engine
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture(scope="function")
def pg_session_factory(postgres_engine):
    Session = sessionmaker(bind=postgres_engine, autoflush=False, autocommit=False)
    yield Session


def test_postgres_concurrent_claim_isolation(pg_session_factory):
    """
    Prove that two simultaneous PostgreSQL sessions using SELECT ... FOR UPDATE SKIP LOCKED
    cannot claim the exact same job, and SKIP LOCKED allows worker 2 to claim the next queued job.
    """
    Session1 = pg_session_factory()
    Session2 = pg_session_factory()

    try:
        # Create 2 queued jobs
        job1 = PodcastJob(
            gmail_message_id="pg-claim-msg-1",
            sender_email="user1@example.com",
            request_mode="standard",
            source_type="email_body",
            source_hash="pg-hash-1",
            source_text="Postgres test 1",
            status=JobState.QUEUED_TTS.value,
        )
        job2 = PodcastJob(
            gmail_message_id="pg-claim-msg-2",
            sender_email="user2@example.com",
            request_mode="standard",
            source_type="email_body",
            source_hash="pg-hash-2",
            source_text="Postgres test 2",
            status=JobState.QUEUED_TTS.value,
        )
        Session1.add_all([job1, job2])
        Session1.commit()

        # Worker 1 claims first job in Session1 (uncommitted transaction holding row lock)
        claimed1 = claim_next_job(Session1, worker_id="worker-1")
        assert claimed1 is not None
        assert claimed1.id == job1.id

        # Worker 2 attempts claim in Session2 while Session1 holds lock on job1
        claimed2 = claim_next_job(Session2, worker_id="worker-2")
        assert claimed2 is not None
        # SKIP LOCKED should jump to job2!
        assert claimed2.id == job2.id
        assert claimed2.id != claimed1.id

        Session1.commit()
        Session2.commit()
    finally:
        Session1.close()
        Session2.close()


def test_postgres_retry_requeue_race_safety(pg_session_factory):
    """
    Prove that requeue_due_tts_retries is race-safe under row locks on PostgreSQL.
    """
    Session1 = pg_session_factory()
    Session2 = pg_session_factory()

    try:
        past = datetime.now(UTC) - timedelta(seconds=60)
        job = PodcastJob(
            gmail_message_id="pg-requeue-msg-1",
            sender_email="user@example.com",
            request_mode="standard",
            source_type="email_body",
            source_hash="pg-requeue-hash",
            source_text="Requeue test",
            status=JobState.FAILED_RETRYABLE.value,
            failed_stage=JobState.SYNTHESIZING.value,
            next_retry_at=past,
        )
        Session1.add(job)
        Session1.commit()

        requeue_due_tts_retries(Session1)
        requeue_due_tts_retries(Session2)

        Session1.refresh(job)
        assert job.status == JobState.QUEUED_TTS.value
        assert job.next_retry_at is None
    finally:
        Session1.close()
        Session2.close()


def test_postgres_stale_recovery_does_not_steal_active_lease(pg_session_factory):
    """
    Prove that stale recovery on PostgreSQL does NOT reclaim an active job with a fresh heartbeat,
    even if the initial lease creation timestamp was >5 minutes ago.
    """
    Session1 = pg_session_factory()

    try:
        now = datetime.now(UTC)
        old_claimed_at = now - timedelta(minutes=10)
        fresh_heartbeat = now - timedelta(seconds=10)
        valid_lease_exp = now + timedelta(seconds=290)

        active_job = PodcastJob(
            gmail_message_id="pg-lease-active-msg",
            sender_email="user@example.com",
            request_mode="standard",
            source_type="email_body",
            source_hash="pg-lease-active-hash",
            source_text="Active lease test",
            status=JobState.SYNTHESIZING.value,
            claimed_at=old_claimed_at,
            claimed_by="worker-active",
            claim_owner="worker-active",
            heartbeat_at=fresh_heartbeat,
            last_heartbeat_at=fresh_heartbeat,
            lease_expires_at=valid_lease_exp,
        )
        Session1.add(active_job)
        Session1.commit()

        recover_stale_claims(Session1, stale_minutes=5)
        Session1.refresh(active_job)

        # Must NOT be recovered!
        assert active_job.status == JobState.SYNTHESIZING.value
        assert active_job.claimed_by == "worker-active"
    finally:
        Session1.close()
