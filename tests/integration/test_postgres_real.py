import os
import threading
import uuid
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
from herald.db.models import JobState, PodcastJob, PodcastTTSChunk


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


def test_postgres_shared_tts_slot_concurrency(pg_session_factory, monkeypatch, tmp_path):
    """
    Prove real PostgreSQL advisory lock slot pool across actual application paths:
    - Worker chunk synthesis path (synthesize_single_chunk)
    - Telegram voice sample synthesis path (ensure_voice_sample)
    Given effective global slots = 2:
    - Worker chunk synthesis occupies slot 1
    - Voice sample generation occupies slot 2
    - Third worker/sample inference attempt times out because all slots are busy
    - Releasing one slot allows the waiting inference to proceed immediately
    - Exceptions inside worker/sample blocks cleanly release advisory locks
    """
    from pathlib import Path
    from unittest.mock import MagicMock

    from herald.concurrency import get_semaphores, reset_semaphores_for_tests, tts_slot_lock
    from herald.config import settings
    from herald.services.voice_manager import ensure_voice_sample
    from herald.tts.chunk_manager import TTSChunk, synthesize_single_chunk
    from herald.tts.kokoro_client import KokoroClient

    monkeypatch.setattr(settings, "HERALD_TTS_GLOBAL_SLOTS", 2)
    monkeypatch.setattr(settings, "HERALD_TTS_SLOT_BASE", 930000)
    monkeypatch.setattr(
        "herald.tts.chunk_manager.validate_audio_file",
        lambda p: {"duration_seconds": 1.5, "size_bytes": 100},
    )
    reset_semaphores_for_tests()

    SessionWorker = pg_session_factory()
    SessionVoice = pg_session_factory()
    SessionThird = pg_session_factory()

    try:
        # Create test job and chunk record in DB
        job = PodcastJob(
            id=f"pg-slot-{uuid.uuid4().hex[:16]}",
            transport="telegram",
            source_hash="pg-sh-1",
            source_text="Worker chunk text",
        )
        SessionWorker.add(job)
        c1 = PodcastTTSChunk(
            job_id=job.id,
            chunk_index=1,
            text_hash="pg-th-1",
            status="PENDING",
        )
        SessionWorker.add(c1)
        SessionWorker.commit()

        worker_in_synth = threading.Event()
        worker_can_finish = threading.Event()
        voice_in_synth = threading.Event()
        voice_can_finish = threading.Event()

        def mock_worker_synth(text, output_path, **kwargs):
            worker_in_synth.set()
            worker_can_finish.wait(timeout=10.0)
            Path(output_path).write_bytes(
                b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
            )

        def mock_voice_synth(text, output_path, **kwargs):
            voice_in_synth.set()
            voice_can_finish.wait(timeout=10.0)
            Path(output_path).write_bytes(
                b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
            )

        mock_kokoro_w = MagicMock(spec=KokoroClient)
        mock_kokoro_w.synthesize_chunk.side_effect = mock_worker_synth

        mock_kokoro_v = MagicMock(spec=KokoroClient)
        mock_kokoro_v.synthesize_chunk.side_effect = mock_voice_synth

        sems = get_semaphores()
        chunk_item = TTSChunk(index=1, text="Worker chunk text")

        worker_errors = []
        voice_errors = []

        # Thread 1: Worker executing synthesize_single_chunk with SessionWorker
        def run_worker():
            try:
                synthesize_single_chunk(
                    session_factory=pg_session_factory,
                    job_id=job.id,
                    chunk=chunk_item,
                    voice="af_heart",
                    speed=1.0,
                    synthesis_timeout=10.0,
                    chunks_dir=tmp_path,
                    kokoro_client=mock_kokoro_w,
                    global_semaphore=sems.global_tts,
                    per_job_semaphore=sems.create_per_job_tts_semaphore(),
                    worker_id="pg-worker-1",
                )
            except Exception as e:
                worker_errors.append(e)

        # Thread 2: Voice sample synthesis with SessionVoice
        def run_voice():
            try:
                # Mock get_voice_sample_path to write to tmp_path
                monkeypatch.setattr(
                    "herald.services.voice_manager.get_voice_sample_path",
                    lambda v: tmp_path / f"{v}.mp3",
                )
                monkeypatch.setattr(
                    "herald.services.voice_manager.convert_wav_to_mp3",
                    lambda src, dst: Path(dst).write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00dummy"),
                )
                monkeypatch.setattr(
                    "herald.services.voice_manager.is_valid_sample_audio",
                    lambda p: Path(p).exists() and Path(p).stat().st_size > 0,
                )
                ensure_voice_sample(voice="af_bella", kokoro_client=mock_kokoro_v, db=SessionVoice)
            except Exception as e:
                voice_errors.append(e)

        t_w = threading.Thread(target=run_worker)
        t_v = threading.Thread(target=run_voice)

        t_w.start()
        assert worker_in_synth.wait(timeout=5.0)

        t_v.start()
        assert voice_in_synth.wait(timeout=5.0)

        # Both slots (worker and voice sample) are now busy.
        # A third session attempting inference MUST time out.
        with pytest.raises(TimeoutError, match="busy"):
            with tts_slot_lock(db=SessionThird, timeout_seconds=0.3):
                pass

        # Release worker slot -> Third session can now acquire immediately
        worker_can_finish.set()
        t_w.join(timeout=5.0)

        with tts_slot_lock(db=SessionThird, timeout_seconds=2.0) as slotThird:
            assert slotThird in (930000, 930001)

        voice_can_finish.set()
        t_v.join(timeout=5.0)

        assert len(worker_errors) == 0, f"Worker thread failed: {worker_errors}"
        assert len(voice_errors) == 0, f"Voice sample thread failed: {voice_errors}"

        # Failure inside slot lock cleanly releases advisory lock
        try:
            with tts_slot_lock(db=SessionWorker, timeout_seconds=2.0):
                raise RuntimeError("Test error in worker slot")
        except RuntimeError:
            pass

        with tts_slot_lock(db=SessionWorker, timeout_seconds=2.0) as slotReacquired:
            assert slotReacquired in (930000, 930001)
    finally:
        SessionWorker.close()
        SessionVoice.close()
        SessionThird.close()
        reset_semaphores_for_tests()


def test_postgres_approval_cas_race(pg_session_factory):
    """
    Prove that simultaneous atomic Compare-And-Swap decisions (Approve vs Approve)
    across independent PostgreSQL sessions result in exactly ONE winner, one transition,
    and preserve strict state consistency.
    """
    from herald.db.models import JobStateTransition

    SessionAdmin = pg_session_factory()
    SessionUser1 = pg_session_factory()
    SessionUser2 = pg_session_factory()

    try:
        # 1. Setup job awaiting approval
        job = PodcastJob(
            id=f"pg-cas-aa-{uuid.uuid4().hex[:16]}",
            transport="telegram",
            telegram_user_id=777,
            telegram_chat_id=777,
            request_mode="standard",
            source_hash="pg-cas-h-aa",
            source_text="CAS Approve vs Approve race test",
            status=JobState.AWAITING_APPROVAL.value,
        )
        SessionAdmin.add(job)
        SessionAdmin.commit()

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
                .update(
                    {"status": JobState.QUEUED_TTS.value, "approved_at": now},
                    synchronize_session=False,
                )
            )
            if updated == 1:
                t_rec = JobStateTransition(
                    job_id=job.id,
                    from_state=JobState.AWAITING_APPROVAL.value,
                    to_state=JobState.QUEUED_TTS.value,
                    component="telegram-approval",
                    message="Approved by user",
                    created_at=now,
                )
                session.add(t_rec)
            session.commit()
            results[name] = updated

        t1 = threading.Thread(target=approve_task, args=(SessionUser1, "u1"))
        t2 = threading.Thread(target=approve_task, args=(SessionUser2, "u2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly ONE session won the race
        assert results["u1"] + results["u2"] == 1

        # Verify DB state and transition history
        SessionAdmin.expire_all()
        db_job = SessionAdmin.query(PodcastJob).filter_by(id=job.id).first()
        assert db_job.status == JobState.QUEUED_TTS.value

        transitions = SessionAdmin.query(JobStateTransition).filter_by(job_id=job.id).all()
        assert len(transitions) == 1
        assert transitions[0].to_state == JobState.QUEUED_TTS.value
    finally:
        SessionAdmin.close()
        SessionUser1.close()
        SessionUser2.close()


def test_postgres_approval_vs_cancel_cas_race(pg_session_factory):
    """
    Prove that simultaneous atomic Compare-And-Swap decisions (Approve vs Cancel)
    across independent PostgreSQL sessions result in exactly ONE winner and preserve
    strict state consistency and transition history.
    """
    from herald.db.models import JobStateTransition

    SessionAdmin = pg_session_factory()
    SessionApprove = pg_session_factory()
    SessionCancel = pg_session_factory()

    try:
        # 1. Setup job awaiting approval
        job = PodcastJob(
            id=f"pg-cas-ac-{uuid.uuid4().hex[:16]}",
            transport="telegram",
            telegram_user_id=777,
            telegram_chat_id=777,
            request_mode="standard",
            source_hash="pg-cas-h-ac",
            source_text="CAS Approve vs Cancel race test",
            status=JobState.AWAITING_APPROVAL.value,
        )
        SessionAdmin.add(job)
        SessionAdmin.commit()

        barrier = threading.Barrier(2)
        results = {}

        def approve_task(session):
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
                .update(
                    {"status": JobState.QUEUED_TTS.value, "approved_at": now},
                    synchronize_session=False,
                )
            )
            if updated == 1:
                t_rec = JobStateTransition(
                    job_id=job.id,
                    from_state=JobState.AWAITING_APPROVAL.value,
                    to_state=JobState.QUEUED_TTS.value,
                    component="telegram-approval",
                    message="Approved by user",
                    created_at=now,
                )
                session.add(t_rec)
            session.commit()
            results["approve"] = updated

        def cancel_task(session):
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
                .update(
                    {"status": JobState.CANCELLED.value, "updated_at": now},
                    synchronize_session=False,
                )
            )
            if updated == 1:
                t_rec = JobStateTransition(
                    job_id=job.id,
                    from_state=JobState.AWAITING_APPROVAL.value,
                    to_state=JobState.CANCELLED.value,
                    component="telegram-approval",
                    message="Cancelled by user",
                    created_at=now,
                )
                session.add(t_rec)
            session.commit()
            results["cancel"] = updated

        t1 = threading.Thread(target=approve_task, args=(SessionApprove,))
        t2 = threading.Thread(target=cancel_task, args=(SessionCancel,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly ONE session won the race
        assert results["approve"] + results["cancel"] == 1

        # Verify DB state and transition history
        SessionAdmin.expire_all()
        db_job = SessionAdmin.query(PodcastJob).filter_by(id=job.id).first()
        assert db_job.status in (JobState.QUEUED_TTS.value, JobState.CANCELLED.value)

        transitions = SessionAdmin.query(JobStateTransition).filter_by(job_id=job.id).all()
        assert len(transitions) == 1
        assert transitions[0].to_state == db_job.status
    finally:
        SessionAdmin.close()
        SessionApprove.close()
        SessionCancel.close()


def test_postgres_migration_014_telemetry_and_cascade(pg_session_factory):
    """
    Prove that migration 014 schema additions (job_diagnostic_events table,
    extended ai_interactions columns) work durably on real PostgreSQL,
    and ON DELETE CASCADE cleanly cascades job deletions to diagnostic events and ai_interactions.
    """
    from herald.db.models import AIInteraction, JobDiagnosticEvent

    session = pg_session_factory()
    try:
        job = PodcastJob(
            gmail_message_id="pg-mig014-msg",
            telegram_user_id=888,
            telegram_chat_id=888,
            request_mode="standard",
            source_hash="pg-mig014-hash",
            source_text="Migration 014 verification source",
            status=JobState.COMPLETE.value,
        )
        session.add(job)
        session.commit()

        # Insert AIInteraction with full 014 columns
        interaction = AIInteraction(
            job_id=job.id,
            provider="groq",
            model="llama-3.3-70b-versatile",
            operation="script_generation",
            attempt=1,
            http_status=200,
            provider_request_id="req-pg-014",
            input_chars=150,
            prompt_tokens=40,
            completion_tokens=60,
            total_tokens=100,
            success=True,
            request_json_sanitized={"mode": "standard", "attempt": 1},
            response_json_sanitized={"http_status": 200, "schema_validation": "valid"},
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        session.add(interaction)

        # Insert JobDiagnosticEvent with full 014 columns
        event = JobDiagnosticEvent(
            job_id=job.id,
            timestamp=datetime.now(UTC),
            level="INFO",
            component="test",
            event_type="EXTRACTION_COMPLETE",
            message="Test extraction event on real PostgreSQL",
            metadata_json_sanitized={"url": "https://example.com/test"},
        )
        session.add(event)
        session.commit()

        # Query back and verify persistence
        saved_int = session.query(AIInteraction).filter_by(job_id=job.id).first()
        assert saved_int is not None
        assert saved_int.provider == "groq"
        assert saved_int.http_status == 200
        assert saved_int.request_json_sanitized["mode"] == "standard"

        saved_evt = session.query(JobDiagnosticEvent).filter_by(job_id=job.id).first()
        assert saved_evt is not None
        assert saved_evt.event_type == "EXTRACTION_COMPLETE"
        assert saved_evt.metadata_json_sanitized["url"] == "https://example.com/test"

        # Verify ON DELETE CASCADE
        session.delete(job)
        session.commit()

        assert session.query(AIInteraction).filter_by(job_id=job.id).count() == 0
        assert session.query(JobDiagnosticEvent).filter_by(job_id=job.id).count() == 0
    finally:
        session.close()


def test_postgres_alembic_migration_013_to_014_roundtrip(postgres_engine):
    """
    Prove that Alembic migration 013 -> 014 -> 013 -> 014 upgrades and downgrades cleanly
    on real PostgreSQL in an isolated schema without schema errors, data loss, or dangling constraints.
    """
    import uuid

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    schema_name = f"test_schema_mig_{uuid.uuid4().hex[:8]}"

    with postgres_engine.connect() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema_name}"))
        conn.commit()

    try:
        alembic_cfg = Config("alembic.ini")
        base_url = postgres_engine.url
        query_params = dict(base_url.query)
        query_params["options"] = f"-csearch_path={schema_name}"
        schema_url_obj = base_url.set(query=query_params)
        url_with_schema = schema_url_obj.render_as_string(hide_password=False)
        alembic_cfg.set_main_option("sqlalchemy.url", url_with_schema)

        # 1. Upgrade from scratch up to 013_ai_interactions
        command.upgrade(alembic_cfg, "013_ai_interactions")

        # 2. Insert PodcastJob + pre-014 AIInteraction in isolated schema
        job_id = f"job-mig-{uuid.uuid4().hex[:8]}"
        int_id = f"int-mig-{uuid.uuid4().hex[:8]}"
        with postgres_engine.connect() as conn:
            conn.execute(text(f"SET search_path TO {schema_name}"))
            conn.execute(
                text(
                    """
                    INSERT INTO podcast_jobs (
                        id, gmail_message_id, sender_email, request_mode, source_type,
                        source_hash, source_text, status, created_at, updated_at
                    ) VALUES (
                        :id, 'msg-mig-013', 'user@example.com', 'standard', 'text',
                        'hash-mig', 'source mig text', 'COMPLETE', NOW(), NOW()
                    )
                    """
                ),
                {"id": job_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO ai_interactions (
                        id, job_id, provider, model, operation, started_at, success
                    ) VALUES (
                        :id, :job_id, 'groq', 'llama-3.3-70b-versatile', 'script_generation', NOW(), true
                    )
                    """
                ),
                {"id": int_id, "job_id": job_id},
            )
            conn.commit()

        # 3. Upgrade to 014_diag_events
        command.upgrade(alembic_cfg, "014_diag_events")

        # 4. Verify pre-existing data survived, new 014 columns accept data, JobDiagnosticEvent insert works, ON DELETE CASCADE works
        event_id = f"evt-mig-{uuid.uuid4().hex[:8]}"
        with postgres_engine.connect() as conn:
            conn.execute(text(f"SET search_path TO {schema_name}"))
            # Pre-existing survived
            res_int = conn.execute(
                text("SELECT id, provider, http_status FROM ai_interactions WHERE id = :id"),
                {"id": int_id},
            ).fetchone()
            assert res_int is not None
            assert res_int[1] == "groq"
            assert res_int[2] is None

            # Update new 014 column on pre-existing row
            conn.execute(
                text(
                    """
                    UPDATE ai_interactions
                    SET http_status = 200, request_json_sanitized = '{"mode": "standard"}'::json
                    WHERE id = :id
                    """
                ),
                {"id": int_id},
            )
            conn.commit()

            # Insert JobDiagnosticEvent
            conn.execute(
                text(
                    """
                    INSERT INTO job_diagnostic_events (
                        id, job_id, timestamp, level, component, event_type, message, metadata_json_sanitized
                    ) VALUES (
                        :id, :job_id, NOW(), 'INFO', 'test', 'EXTRACTION_COMPLETE', 'Test mig msg', '{"k": "v"}'::json
                    )
                    """
                ),
                {"id": event_id, "job_id": job_id},
            )
            conn.commit()

            res_evt = conn.execute(
                text("SELECT id, event_type, metadata_json_sanitized FROM job_diagnostic_events WHERE id = :id"),
                {"id": event_id},
            ).fetchone()
            assert res_evt is not None
            assert res_evt[1] == "EXTRACTION_COMPLETE"

            # Verify FK ON DELETE CASCADE
            conn.execute(text("DELETE FROM podcast_jobs WHERE id = :job_id"), {"job_id": job_id})
            conn.commit()

            cnt_int = conn.execute(text("SELECT count(*) FROM ai_interactions WHERE job_id = :job_id"), {"job_id": job_id}).scalar()
            cnt_evt = conn.execute(text("SELECT count(*) FROM job_diagnostic_events WHERE job_id = :job_id"), {"job_id": job_id}).scalar()
            assert cnt_int == 0
            assert cnt_evt == 0

        # 5. Downgrade to 013_ai_interactions
        command.downgrade(alembic_cfg, "013_ai_interactions")

        # 6. Re-upgrade to 014_diag_events (roundtrip)
        command.upgrade(alembic_cfg, "014_diag_events")

    finally:
        with postgres_engine.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
            conn.commit()

