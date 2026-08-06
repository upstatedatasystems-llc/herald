import pytest

from herald.db.models import JobState, PodcastJob
from herald.db.state_machine import InvalidStateTransitionError, transition_job_state


def test_valid_state_transitions(db_session):
    job = PodcastJob(
        gmail_message_id="msg-101",
        sender_email="user@example.com",
        source_hash="hash123",
        source_text="Test source content",
        status=JobState.RECEIVED.value,
    )
    db_session.add(job)
    db_session.commit()

    # RECEIVED -> VALIDATING
    job = transition_job_state(db_session, job, JobState.VALIDATING.value, component="test")
    assert job.status == JobState.VALIDATING.value
    assert len(job.transitions) == 1

    # VALIDATING -> EXTRACTING
    job = transition_job_state(db_session, job, JobState.EXTRACTING.value, component="test")
    assert job.status == JobState.EXTRACTING.value

    # EXTRACTING -> SOURCE_READY
    job = transition_job_state(db_session, job, JobState.SOURCE_READY.value, component="test")
    assert job.status == JobState.SOURCE_READY.value


def test_invalid_state_transition(db_session):
    job = PodcastJob(
        gmail_message_id="msg-102",
        sender_email="user@example.com",
        source_hash="hash456",
        source_text="Test content",
        status=JobState.RECEIVED.value,
    )
    db_session.add(job)
    db_session.commit()

    # Cannot transition directly from RECEIVED to COMPLETE
    with pytest.raises(InvalidStateTransitionError):
        transition_job_state(db_session, job, JobState.COMPLETE.value, component="test")
