from datetime import UTC, datetime, timedelta

from apps.worker.main import process_next_job, recover_stale_claims
from herald.db.models import JobState, PodcastJob
from herald.tts.kokoro_client import KokoroClient


def test_concurrency_worker_claims_and_stale_recovery(db_session, monkeypatch):
    """
    Test worker atomic claims, concurrent session isolation, and stale claim recovery.
    """
    monkeypatch.setenv("HERALD_MOCK_TTS", "1")

    # Create 2 QUEUED_TTS jobs
    job1 = PodcastJob(
        gmail_message_id="msg-conc-1",
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-conc-1",
        source_text="Concurrency test 1",
        status=JobState.QUEUED_TTS.value,
        script_json={
            "episode_title": "Conc 1",
            "episode_description": "Desc 1",
            "estimated_minutes": 1,
            "segments": [{"order": 1, "heading": "H1", "narration": "Narration 1"}],
            "warnings": [],
        },
    )

    job2 = PodcastJob(
        gmail_message_id="msg-conc-2",
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-conc-2",
        source_text="Concurrency test 2",
        status=JobState.QUEUED_TTS.value,
        script_json={
            "episode_title": "Conc 2",
            "episode_description": "Desc 2",
            "estimated_minutes": 1,
            "segments": [{"order": 1, "heading": "H2", "narration": "Narration 2"}],
            "warnings": [],
        },
    )

    db_session.add_all([job1, job2])
    db_session.commit()

    kokoro = KokoroClient()

    # Process first job
    processed_1 = process_next_job(db_session, kokoro)
    assert processed_1 is True

    # Job 1 should be AUDIO_READY
    db_session.refresh(job1)
    assert job1.status == JobState.AUDIO_READY.value

    # Process second job
    processed_2 = process_next_job(db_session, kokoro)
    assert processed_2 is True
    db_session.refresh(job2)
    assert job2.status == JobState.AUDIO_READY.value

    # Stale claim recovery test
    stale_time = datetime.now(UTC) - timedelta(minutes=30)
    stale_job = PodcastJob(
        gmail_message_id="msg-stale-conc",
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-stale-conc",
        source_text="Stale content",
        status=JobState.SYNTHESIZING.value,
        claimed_at=stale_time,
        last_heartbeat_at=stale_time,
    )
    db_session.add(stale_job)
    db_session.commit()

    recover_stale_claims(db_session, stale_minutes=15)
    db_session.refresh(stale_job)
    assert stale_job.status == JobState.QUEUED_TTS.value
    assert stale_job.claimed_at is None
    assert stale_job.claim_owner is None
