from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from apps.worker.main import (
    claim_next_scripting_job,
    recover_stale_claims,
)
import apps.worker.main
from herald.db.models import JobState, PodcastJob


def test_claim_next_scripting_job_commit_precedes_diagnostic(db_session):
    """
    Verify that in claim_next_scripting_job, the parent podcast_jobs row mutation
    is committed (db.commit()) BEFORE the isolated telemetry call (record_job_diagnostic_event).
    This guarantees no PostgreSQL foreign key check self-deadlock occurs.
    """
    job = PodcastJob(
        gmail_message_id="msg-order-script-1",
        sender_email="order@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-order-1",
        source_text="Sample text for order test",
        status=JobState.SCRIPTING.value,
        claimed_by=None,
    )
    db_session.add(job)
    db_session.commit()

    call_order = []
    orig_commit = db_session.commit

    def tracked_commit():
        call_order.append(("commit",))
        return orig_commit()

    db_session.commit = tracked_commit

    with patch("apps.worker.main.record_job_diagnostic_event") as mock_diag:
        def fake_record(*args, **kwargs):
            event_type = args[3] if len(args) > 3 else kwargs.get("event_type")
            call_order.append(("diagnostic", event_type))
            return None

        mock_diag.side_effect = fake_record

        claimed = claim_next_scripting_job(db_session, worker_id="test-worker-order", lease_seconds=300)

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.claimed_by == "test-worker-order"

    commit_indices = [i for i, c in enumerate(call_order) if c[0] == "commit"]
    diag_indices = [i for i, c in enumerate(call_order) if c[0] == "diagnostic" and c[1] == "SCRIPTING_CLAIMED"]

    assert len(commit_indices) >= 1, "db.commit() must be called"
    assert len(diag_indices) == 1, "record_job_diagnostic_event(SCRIPTING_CLAIMED) must be called once"
    assert commit_indices[0] < diag_indices[0], (
        f"db.commit() (index {commit_indices[0]}) must strictly precede "
        f"record_job_diagnostic_event (index {diag_indices[0]}) to prevent Postgres deadlock"
    )


def test_recover_stale_scripting_claims_commit_precedes_diagnostic(db_session):
    """
    Verify that in recover_stale_claims for SCRIPTING jobs, db.commit() strictly
    precedes record_job_diagnostic_event(STALE_SCRIPTING_CLAIM_RECOVERED).
    Also verify job remains in SCRIPTING and is immediately claimable.
    """
    old_time = datetime.now(UTC) - timedelta(minutes=30)
    stale_job = PodcastJob(
        gmail_message_id="msg-stale-script-1",
        sender_email="order@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-stale-script-1",
        source_text="Sample text for stale scripting test",
        status=JobState.SCRIPTING.value,
        claimed_by="dead-worker",
        claim_owner="dead-worker",
        claimed_at=old_time,
        last_heartbeat_at=old_time,
        heartbeat_at=old_time,
        lease_expires_at=old_time + timedelta(seconds=300),
    )
    db_session.add(stale_job)
    db_session.commit()

    call_order = []
    orig_commit = db_session.commit

    def tracked_commit():
        call_order.append(("commit",))
        return orig_commit()

    db_session.commit = tracked_commit

    with patch("apps.worker.main.record_job_diagnostic_event") as mock_diag:
        def fake_record(*args, **kwargs):
            event_type = args[3] if len(args) > 3 else kwargs.get("event_type")
            call_order.append(("diagnostic", event_type))
            return None

        mock_diag.side_effect = fake_record

        recover_stale_claims(db_session, stale_minutes=15)

    commit_indices = [i for i, c in enumerate(call_order) if c[0] == "commit"]
    diag_indices = [
        i for i, c in enumerate(call_order)
        if c[0] == "diagnostic" and c[1] == "STALE_SCRIPTING_CLAIM_RECOVERED"
    ]

    assert len(commit_indices) >= 1, "db.commit() must be called on recovery"
    assert len(diag_indices) == 1, "record_job_diagnostic_event(STALE_SCRIPTING_CLAIM_RECOVERED) must be called"
    assert commit_indices[0] < diag_indices[0], (
        f"db.commit() (index {commit_indices[0]}) must strictly precede "
        f"isolated STALE_SCRIPTING_CLAIM_RECOVERED diagnostic (index {diag_indices[0]})"
    )

    db_session.refresh(stale_job)
    assert stale_job.status == JobState.SCRIPTING.value
    assert stale_job.claimed_by is None
    assert stale_job.claim_owner is None
    assert stale_job.lease_expires_at is None

    # Job is immediately re-claimable by another worker
    reclaimed = claim_next_scripting_job(db_session, worker_id="resuming-worker", lease_seconds=300)
    assert reclaimed is not None
    assert reclaimed.id == stale_job.id
    assert reclaimed.claimed_by == "resuming-worker"


def test_recover_stale_claims_commit_precedes_lease_recovery_metric(db_session):
    """
    Verify that in recover_stale_claims for non-SCRIPTING jobs (e.g. SYNTHESIZING),
    authoritative state transition is committed (db.commit()) BEFORE recording the
    isolated LEASE_RECOVERY stage metric.
    """
    old_time = datetime.now(UTC) - timedelta(minutes=30)
    stale_job = PodcastJob(
        gmail_message_id="msg-stale-synth-1",
        sender_email="order@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-stale-synth-1",
        source_text="Sample text for stale synth test",
        status=JobState.SYNTHESIZING.value,
        synthesis_attempt_count=1,
        claimed_by="dead-synth-worker",
        claim_owner="dead-synth-worker",
        claimed_at=old_time,
        last_heartbeat_at=old_time,
        lease_expires_at=old_time + timedelta(seconds=300),
    )
    db_session.add(stale_job)
    db_session.commit()

    call_order = []
    orig_commit = db_session.commit

    def tracked_commit():
        call_order.append(("commit",))
        return orig_commit()

    db_session.commit = tracked_commit

    with patch("apps.worker.main.record_stage_metric") as mock_metric:
        def fake_metric(*args, **kwargs):
            stage = kwargs.get("stage") or (args[1] if len(args) > 1 else None)
            call_order.append(("metric", stage))
            return None

        mock_metric.side_effect = fake_metric

        recover_stale_claims(db_session, stale_minutes=15)

    commit_indices = [i for i, c in enumerate(call_order) if c[0] == "commit"]
    metric_indices = [
        i for i, c in enumerate(call_order)
        if c[0] == "metric" and c[1] == "LEASE_RECOVERY"
    ]

    assert len(commit_indices) >= 1, "Authoritative transition must be committed"
    assert len(metric_indices) == 1, "LEASE_RECOVERY stage metric must be called"
    assert commit_indices[0] < metric_indices[0], (
        f"State transition commit (index {commit_indices[0]}) must strictly precede "
        f"isolated LEASE_RECOVERY metric (index {metric_indices[0]})"
    )

    db_session.refresh(stale_job)
    assert stale_job.status == JobState.QUEUED_TTS.value
    assert stale_job.claimed_by is None
