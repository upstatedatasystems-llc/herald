import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from herald.db.models import JobDiagnosticEvent, JobState, PodcastJob
from herald.db.state_machine import transition_job_state
from herald.services.diagnostics_export import ensure_terminal_diagnostics_archive

logger = logging.getLogger("herald.services.recovery")


def ops_stale_recovery(db: Session) -> dict:
    """
    Recover stale jobs across all active stages using stage-specific timeouts.
    """
    now = datetime.now(UTC)
    recovered = 0

    stale_specs = [
        (JobState.EXTRACTING.value, timedelta(minutes=15), JobState.FAILED_FINAL.value),
        (JobState.SYNTHESIZING.value, timedelta(minutes=15), JobState.QUEUED_TTS.value),
        (JobState.ENCODING.value, timedelta(minutes=15), JobState.QUEUED_TTS.value),
        (JobState.UPLOADING.value, timedelta(minutes=30), JobState.UPLOADING.value),
        (JobState.DELIVERING.value, timedelta(minutes=30), JobState.DELIVERING.value),
    ]

    for status_val, timeout, target_val in stale_specs:
        cutoff = now - timeout
        jobs = (
            db.query(PodcastJob)
            .filter(PodcastJob.status == status_val)
            .with_for_update(skip_locked=True)
            .all()
        )
        for job in jobs:
            if status_val == JobState.EXTRACTING.value and getattr(job, "transport", None) != "telegram":
                continue
            last_active = job.last_heartbeat_at or job.claimed_at or job.updated_at or job.created_at
            if last_active:
                if last_active.tzinfo is None:
                    last_active = last_active.replace(tzinfo=UTC)
                if last_active < cutoff:
                    job.claimed_at = None
                    job.claim_owner = None
                    job.last_heartbeat_at = None
                    if status_val == JobState.EXTRACTING.value and target_val == JobState.FAILED_FINAL.value:
                        job.error_code = "INTAKE_TIMEOUT"
                        job.failed_stage = "EXTRACTING"
                        event = JobDiagnosticEvent(
                            job_id=job.id,
                            component="herald-ops-recovery",
                            event_type="STALE_INTAKE_RECOVERED",
                            message="Stale intake recovered",
                            metadata_json_sanitized={"prior_state": "EXTRACTING", "transport": job.transport},
                        )
                        db.add(event)
                    transition_job_state(
                        db,
                        job,
                        target_val,
                        component="herald-ops-recovery",
                        message="Recovered stale claim via operational recovery workflow",
                        force=True,
                        commit=False,
                    )
                    db.commit()
                    if target_val == JobState.FAILED_FINAL.value:
                        try:
                            ensure_terminal_diagnostics_archive(job.id, JobState.FAILED_FINAL.value)
                        except Exception as e:
                            logger.warning("Failed ensuring terminal diagnostics archive: %s", e)
                    recovered += 1

    db.commit()
    return {"status": "success", "recovered_jobs": recovered}
