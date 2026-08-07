from datetime import UTC, datetime, timedelta
import pytest
from herald.db.models import JobState, PodcastJob, RequestMode, SourceType
from herald.services.eta_calculator import calculate_job_eta


def test_eta_single_episode(db_session):
    now = datetime.now(UTC)
    job = PodcastJob(
        gmail_message_id="eta-msg-1",
        sender_email="auth@example.com",
        request_mode=RequestMode.STANDARD.value,
        source_type=SourceType.EMAIL_BODY.value,
        source_hash="hash1",
        source_text="Test source text",
        status=JobState.QUEUED_TTS.value,
        created_at=now,
        script_json={"estimated_minutes": 6.0, "segments": [{"text": "s1"}]},
    )
    db_session.add(job)
    db_session.commit()

    eta = calculate_job_eta(db_session, job)
    assert eta["job_id"] == job.id
    assert eta["jobs_ahead"] == 0
    # 6.0 mins * 60 * 2.5 + 60 = 900 + 60 = 960s (16 mins) -> "approximately 15–25 minutes"
    assert "approximately" in eta["estimated_completion_range"]


def test_eta_excludes_jobs_behind(db_session):
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    t1 = datetime.now(UTC) - timedelta(minutes=5)
    t2 = datetime.now(UTC)  # Target job
    t3 = datetime.now(UTC) + timedelta(minutes=5)  # Job created after target job

    j0 = PodcastJob(
        gmail_message_id="eta-0",
        sender_email="a@example.com",
        source_hash="h0",
        source_text="s",
        status=JobState.SYNTHESIZING.value,
        created_at=t0,
        completed_chunk_index=2,
        script_json={"estimated_minutes": 5.0, "segments": [{"t": "1"}, {"t": "2"}, {"t": "3"}, {"t": "4"}]},
    )
    target_job = PodcastJob(
        gmail_message_id="eta-target",
        sender_email="a@example.com",
        source_hash="ht",
        source_text="s",
        status=JobState.QUEUED_TTS.value,
        created_at=t2,
        script_json={"estimated_minutes": 4.0},
    )
    j3 = PodcastJob(
        gmail_message_id="eta-behind",
        sender_email="a@example.com",
        source_hash="h3",
        source_text="s",
        status=JobState.QUEUED_TTS.value,
        created_at=t3,
        script_json={"estimated_minutes": 10.0},
    )
    db_session.add_all([j0, target_job, j3])
    db_session.commit()

    eta = calculate_job_eta(db_session, target_job)
    assert eta["jobs_ahead"] == 1  # Only j0 is ahead, j3 is excluded!
