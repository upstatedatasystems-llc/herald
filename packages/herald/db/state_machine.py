from datetime import UTC, datetime

from sqlalchemy.orm import Session

from packages.herald.db.models import JobState, JobStateTransition, PodcastJob

# Mapping of valid target states from a current state
VALID_TRANSITIONS: dict[str, set[str]] = {
    JobState.RECEIVED.value: {
        JobState.VALIDATING.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    },
    JobState.VALIDATING.value: {
        JobState.EXTRACTING.value,
        JobState.SOURCE_READY.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    },
    JobState.EXTRACTING.value: {
        JobState.SOURCE_READY.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    },
    JobState.SOURCE_READY.value: {
        JobState.SCRIPTING.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    },
    JobState.SCRIPTING.value: {
        JobState.SCRIPT_READY.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    },
    JobState.SCRIPT_READY.value: {
        JobState.QUEUED.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    },
    JobState.QUEUED.value: {
        JobState.SYNTHESIZING.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    },
    JobState.SYNTHESIZING.value: {
        JobState.ENCODING.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    },
    JobState.ENCODING.value: {
        JobState.AUDIO_READY.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    },
    JobState.AUDIO_READY.value: {
        JobState.UPLOADING.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    },
    JobState.UPLOADING.value: {
        JobState.DELIVERING.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    },
    JobState.DELIVERING.value: {
        JobState.COMPLETE.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
    },
    # Allow retries from FAILED back into specific states
    JobState.FAILED.value: {
        JobState.QUEUED.value,
        JobState.SCRIPTING.value,
        JobState.EXTRACTING.value,
        JobState.UPLOADING.value,
        JobState.DELIVERING.value,
        JobState.CANCELLED.value,
    },
    JobState.COMPLETE.value: set(),
    JobState.CANCELLED.value: set(),
}


class InvalidStateTransitionError(Exception):
    pass


def transition_job_state(
    db: Session,
    job: PodcastJob,
    to_state: str,
    component: str,
    message: str | None = None,
    error_category: str | None = None,
    force: bool = False,
) -> PodcastJob:
    """
    Safely transition a job to a new state and record the transition history.
    """
    from_state = job.status
    allowed_targets = VALID_TRANSITIONS.get(from_state, set())

    if not force and to_state not in allowed_targets:
        raise InvalidStateTransitionError(
            f"Cannot transition job {job.id} from '{from_state}' to '{to_state}'"
        )

    job.status = to_state
    job.updated_at = datetime.now(UTC)

    if to_state == JobState.COMPLETE.value:
        job.completed_at = datetime.now(UTC)
    elif to_state == JobState.FAILED.value:
        if error_category:
            job.error_code = error_category
        if message:
            job.error_detail = message

    transition = JobStateTransition(
        job_id=job.id,
        from_state=from_state,
        to_state=to_state,
        component=component,
        message=message,
        error_category=error_category,
    )
    db.add(transition)
    db.commit()
    db.refresh(job)
    return job
