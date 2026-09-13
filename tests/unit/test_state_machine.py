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


def test_awaiting_configuration_transitions(db_session):
    job = PodcastJob(
        telegram_chat_id=123,
        telegram_message_id=456,
        source_hash="hash789",
        source_text="Test content",
        status=JobState.RECEIVED.value,
    )
    db_session.add(job)
    db_session.commit()

    # Flow: RECEIVED -> VALIDATING -> EXTRACTING -> AWAITING_CONFIGURATION -> SCRIPTING
    job = transition_job_state(db_session, job, JobState.VALIDATING.value, component="test")
    job = transition_job_state(db_session, job, JobState.EXTRACTING.value, component="test")
    job = transition_job_state(db_session, job, JobState.AWAITING_CONFIGURATION.value, component="test")
    assert job.status == JobState.AWAITING_CONFIGURATION.value

    # AWAITING_CONFIGURATION -> SCRIPTING
    job = transition_job_state(db_session, job, JobState.SCRIPTING.value, component="test")
    assert job.status == JobState.SCRIPTING.value


def test_invalid_failure_to_awaiting_configuration(db_session):
    job = PodcastJob(
        telegram_chat_id=123,
        telegram_message_id=457,
        source_hash="hash999",
        source_text="Test content",
        status=JobState.FAILED_FINAL.value,
    )
    db_session.add(job)
    db_session.commit()

    # Unrelated FAILED_FINAL cannot jump back to AWAITING_CONFIGURATION
    with pytest.raises(InvalidStateTransitionError):
        transition_job_state(db_session, job, JobState.AWAITING_CONFIGURATION.value, component="test")

