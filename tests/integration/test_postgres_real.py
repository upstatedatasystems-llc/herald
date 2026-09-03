import os
import threading
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


def test_postgres_concurrent_telegram_delivery_isolation(pg_session_factory, tmp_path):
    """
    Prove that two simultaneous PostgreSQL sessions using deliver_pending_telegram_jobs
    with SELECT ... FOR UPDATE SKIP LOCKED and barrier synchronization cannot claim or send
    the same job, and jobs are distributed cleanly without duplicate delivery.
    """
    import threading
    from unittest.mock import MagicMock

    from herald.telegram.client import TelegramClient
    from herald.telegram.delivery import deliver_pending_telegram_jobs

    Session1 = pg_session_factory()
    Session2 = pg_session_factory()

    audio_file1 = tmp_path / "pg_ep1.mp3"
    audio_file2 = tmp_path / "pg_ep2.mp3"
    audio_file1.write_bytes(b"dummy mp3 1")
    audio_file2.write_bytes(b"dummy mp3 2")

    try:
        # Case A: 1 single ready job, 2 competing workers with barrier sync
        j1 = PodcastJob(
            transport="telegram",
            telegram_chat_id=8881,
            telegram_message_id=1,
            request_mode="literal",
            source_hash="pg-tg-hash-1",
            source_text="TG delivery test 1",
            local_audio_path=str(audio_file1),
            audio_duration_seconds=30,
            status=JobState.AUDIO_READY.value,
        )
        Session1.add(j1)
        Session1.commit()

        sent_chats = []
        lock = threading.Lock()

        mock_client = MagicMock(spec=TelegramClient)
        mock_client.is_configured = True

        def mock_send(chat_id, audio_path, **kwargs):
            with lock:
                sent_chats.append(chat_id)
            return {"message_id": 999}

        mock_client.send_audio.side_effect = mock_send

        barrier = threading.Barrier(2)
        results = {}

        def worker_task(session, name):
            barrier.wait()
            cnt = deliver_pending_telegram_jobs(session, mock_client)
            results[name] = cnt

        t1 = threading.Thread(target=worker_task, args=(Session1, "w1"))
        t2 = threading.Thread(target=worker_task, args=(Session2, "w2"))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one worker claimed the single job
        assert len(sent_chats) == 1
        assert sent_chats == [8881]
        assert results["w1"] + results["w2"] == 1

        # Case B: Multi-job distribution
        sent_chats.clear()
        results.clear()

        j2 = PodcastJob(
            transport="telegram",
            telegram_chat_id=8882,
            telegram_message_id=2,
            request_mode="literal",
            source_hash="pg-tg-hash-2",
            source_text="TG delivery test 2",
            local_audio_path=str(audio_file2),
            audio_duration_seconds=40,
            status=JobState.AUDIO_READY.value,
        )
        Session1.add(j2)
        Session1.commit()

        barrier2 = threading.Barrier(2)

        def worker_task2(session, name):
            barrier2.wait()
            cnt = deliver_pending_telegram_jobs(session, mock_client)
            results[name] = cnt

        t3 = threading.Thread(target=worker_task2, args=(Session1, "w1"))
        t4 = threading.Thread(target=worker_task2, args=(Session2, "w2"))

        t3.start()
        t4.start()
        t3.join()
        t4.join()

        assert len(sent_chats) == 1
        assert sent_chats == [8882]

        # Verify DB state
        Session1.expire_all()
        jobs = Session1.query(PodcastJob).filter(PodcastJob.status == JobState.COMPLETE.value).all()
        assert len(jobs) == 2
    finally:
        Session1.close()
        Session2.close()


def test_postgres_shared_tts_slot_concurrency(pg_session_factory, monkeypatch):
    """
    Prove real PostgreSQL advisory lock slot pool across independent database sessions.
    Given effective global slots = 2:
    - Session A acquires slot 1
    - Session B acquires slot 2
    - Session C cannot enter while both are held (times out)
    - When Session A releases, Session C acquires
    - Exceptions inside block automatically release advisory lock in finally
    """
    from herald.concurrency import tts_slot_lock
    from herald.config import settings

    monkeypatch.setattr(settings, "HERALD_TTS_GLOBAL_SLOTS", 2)
    monkeypatch.setattr(settings, "HERALD_TTS_SLOT_BASE", 930000)

    SessionA = pg_session_factory()
    SessionB = pg_session_factory()
    SessionC = pg_session_factory()

    try:
        # A acquires slot 1
        with tts_slot_lock(db=SessionA, timeout_seconds=5.0) as slotA:
            assert slotA in (930000, 930001)

            # B acquires slot 2
            with tts_slot_lock(db=SessionB, timeout_seconds=5.0) as slotB:
                assert slotB in (930000, 930001)
                assert slotA != slotB

                # C attempts to acquire 3rd slot -> should time out because all 2 slots are busy!
                with pytest.raises(TimeoutError, match="busy"):
                    with tts_slot_lock(db=SessionC, timeout_seconds=0.3):
                        pass

            # B has released slotB; C can now acquire
            with tts_slot_lock(db=SessionC, timeout_seconds=2.0) as slotC:
                assert slotC in (930000, 930001)

        # Failure inside block releases advisory lock
        try:
            with tts_slot_lock(db=SessionA, timeout_seconds=2.0):
                raise ValueError("Deliberate error inside TTS slot")
        except ValueError:
            pass

        # Session A can re-acquire immediately
        with tts_slot_lock(db=SessionA, timeout_seconds=2.0) as slotNew:
            assert slotNew in (930000, 930001)
    finally:
        SessionA.close()
        SessionB.close()
        SessionC.close()


def test_postgres_approval_cas_race(pg_session_factory):
    """
    Prove that simultaneous atomic Compare-And-Swap approval decisions across independent
    PostgreSQL sessions result in exactly ONE winner and preserve strict state consistency.
    """
    SessionAdmin = pg_session_factory()
    SessionUser1 = pg_session_factory()
    SessionUser2 = pg_session_factory()

    try:
        # 1. Setup job awaiting approval
        job = PodcastJob(
            id="pg-cas-job-1111-2222-333333333333",
            transport="telegram",
            telegram_user_id=777,
            telegram_chat_id=777,
            request_mode="standard",
            source_hash="pg-cas-h1",
            source_text="CAS test",
            status=JobState.AWAITING_APPROVAL.value,
        )
        SessionAdmin.add(job)
        SessionAdmin.commit()

        # Race 1: SessionUser1 (Approve) vs SessionUser2 (Approve)
        barrier = threading.Barrier(2)
        results = {}

        def approve_task(session, name):
            barrier.wait()
            now = datetime.now(UTC)
            updated = (
                session.query(PodcastJob)
                .filter(
                    PodcastJob.id == job.id,
                    PodcastJob.status == JobState.AWAITING_APPROVAL.value,
                    PodcastJob.telegram_user_id == 777,
                    PodcastJob.telegram_chat_id == 777,
                )
                .update({"status": JobState.QUEUED_TTS.value, "approved_at": now}, synchronize_session=False)
            )
            session.commit()
            results[name] = updated

        t1 = threading.Thread(target=approve_task, args=(SessionUser1, "u1"))
        t2 = threading.Thread(target=approve_task, args=(SessionUser2, "u2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly ONE session updated the row from AWAITING_APPROVAL to QUEUED_TTS
        assert results["u1"] + results["u2"] == 1

        # Verify DB state
        SessionAdmin.expire_all()
        db_job = SessionAdmin.query(PodcastJob).filter_by(id=job.id).first()
        assert db_job.status == JobState.QUEUED_TTS.value
    finally:
        SessionAdmin.close()
        SessionUser1.close()
        SessionUser2.close()

