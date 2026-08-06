from datetime import UTC, datetime, timedelta

from apps.worker.main import process_next_job, recover_stale_claims
from herald.db.models import JobState, PodcastJob
from herald.tts.kokoro_client import KokoroClient


def test_atomic_worker_claim_and_stale_recovery(db_session, monkeypatch):
    monkeypatch.setenv("HERALD_MOCK_TTS", "1")

    job = PodcastJob(
        gmail_message_id="msg-worker-claim-1",
        sender_email="test@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-worker-1",
        source_text="Sample text content for worker claim test.",
        status=JobState.QUEUED_TTS.value,
        script_json={
            "episode_title": "Worker Test",
            "episode_description": "Testing worker claims",
            "estimated_minutes": 5,
            "segments": [{"order": 1, "heading": "Intro", "narration": "Hello world."}],
            "warnings": [],
        },
    )
    db_session.add(job)
    db_session.commit()

    kokoro = KokoroClient()
    processed = process_next_job(db_session, kokoro)
    assert processed is True

    db_session.refresh(job)
    assert job.status == JobState.AUDIO_READY.value
    assert job.synthesis_attempt_count >= 1


def test_stale_claim_recovery_respects_heartbeat(db_session):
    now = datetime.now(UTC)
    old_time = now - timedelta(minutes=30)

    # Job 1: Old timestamp -> Stale
    job_stale = PodcastJob(
        gmail_message_id="msg-stale-1",
        sender_email="test@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-stale-1",
        source_text="Stale text",
        status=JobState.SYNTHESIZING.value,
        claimed_at=old_time,
        last_heartbeat_at=old_time,
    )
    # Job 2: Recent timestamp -> Active heartbeat
    job_active = PodcastJob(
        gmail_message_id="msg-active-1",
        sender_email="test@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-active-1",
        source_text="Active text",
        status=JobState.SYNTHESIZING.value,
        claimed_at=now,
        last_heartbeat_at=now,
    )
    db_session.add_all([job_stale, job_active])
    db_session.commit()

    recover_stale_claims(db_session, stale_minutes=15)

    db_session.refresh(job_stale)
    db_session.refresh(job_active)

    assert job_stale.status == JobState.QUEUED_TTS.value
    assert job_stale.claimed_at is None

    assert job_active.status == JobState.SYNTHESIZING.value
    assert job_active.claimed_at is not None
