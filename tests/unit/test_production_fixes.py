import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from herald.audio.artifact_generator import ensure_details_artifact, get_required_artifact_types
from herald.config import settings
from herald.db.models import JobProcessingMetric, JobState, PodcastJob, RequestMode, SourceType
from herald.services.performance_metrics import record_stage_metric


def test_module_imports():
    """Requirement 1 & 8: Verify modules import cleanly without missing symbols."""
    import apps.api.main
    import apps.worker.main
    import herald.audio.artifact_generator

    assert hasattr(herald.audio.artifact_generator, "ensure_details_artifact")
    assert hasattr(apps.api.main, "claim_delivery_job")
    assert hasattr(apps.worker.main, "run_worker_loop")


def test_gmail_trigger_simple_false_contract():
    """Requirement 2 & 8: Verify email-intake.json Gmail Trigger has simple=false and correct mappings."""
    workflow_path = Path("n8n/workflows/email-intake.json")
    assert workflow_path.exists()

    data = json.loads(workflow_path.read_text(encoding="utf-8"))
    nodes = data.get("nodes", [])

    gmail_node = next((n for n in nodes if n.get("type") == "n8n-nodes-base.gmailTrigger"), None)
    assert gmail_node is not None
    assert gmail_node["parameters"].get("simple") is False

    intake_node = next((n for n in nodes if n.get("name") == "Call Herald API - Intake"), None)
    assert intake_node is not None

    body_params = intake_node["parameters"].get("bodyParameters", {}).get("parameters", [])
    param_names = [p["name"] for p in body_params]

    assert "gmail_message_id" in param_names
    assert "sender_email" in param_names
    assert "subject" in param_names
    assert "body_text" in param_names
    assert "body_html" in param_names
    assert "gmail_received_at" in param_names


def test_completion_dispatcher_credentials_and_artifacts():
    """Requirement 3 & 8: Verify completion-dispatcher.json uses googleDriveOAuth2Api and handles Audio+Details."""
    workflow_path = Path("n8n/workflows/completion-dispatcher.json")
    assert workflow_path.exists()

    data = json.loads(workflow_path.read_text(encoding="utf-8"))
    nodes = data.get("nodes", [])

    drive_nodes = [n for n in nodes if n.get("type") == "n8n-nodes-base.googleDrive"]
    assert len(drive_nodes) >= 2

    for dn in drive_nodes:
        creds = dn.get("credentials", {})
        assert "googleDriveOAuth2Api" in creds
        assert "googleDriveOAuth2" not in creds

    node_names = [n.get("name") for n in nodes]
    assert "Upload Audio to Google Drive" in node_names
    assert "Upload Details to Google Drive" in node_names

    # Old sidecars must NOT be present as upload nodes
    assert "Upload Source to Google Drive" not in node_names
    assert "Upload Script to Google Drive" not in node_names
    assert "Upload Diagnostics to Google Drive" not in node_names


def test_delivery_claim_single_row_lock_and_metrics_ordering(db_session: Session, monkeypatch):
    """Requirement 4, 5 & 8: Verify claim locks 1 eligible job and records metrics without self-deadlock."""
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")
    from apps.api.main import app

    now = datetime.now(UTC)

    # Job 1: Eligible
    job1 = PodcastJob(
        id="job-claim-lock-001",
        gmail_message_id="msg-cl-1",
        sender_email="auth@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hashcl1",
        source_text="Sample text for podcast job 1",
        status=JobState.AUDIO_READY.value,
        created_at=now - timedelta(minutes=5),
        audio_ready_at=now - timedelta(minutes=1),
        updated_at=now - timedelta(minutes=5),
    )

    # Job 2: Eligible
    job2 = PodcastJob(
        id="job-claim-lock-002",
        gmail_message_id="msg-cl-2",
        sender_email="auth@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hashcl2",
        source_text="Sample text for podcast job 2",
        status=JobState.AUDIO_READY.value,
        created_at=now - timedelta(minutes=4),
        audio_ready_at=now - timedelta(minutes=1),
        updated_at=now - timedelta(minutes=4),
    )

    db_session.add_all([job1, job2])
    db_session.commit()

    client = TestClient(app)

    response = client.post("/api/v1/delivery/claim")
    assert response.status_code == 200

    data = response.json()
    assert data["claimed"] is True
    claimed_id = data["job"]["id"]
    assert claimed_id == "job-claim-lock-001"

    # Verify metric was recorded post-commit
    m = (
        db_session.query(JobProcessingMetric)
        .filter(JobProcessingMetric.job_id == claimed_id, JobProcessingMetric.stage == "DELIVERY_DISPATCH_WAIT")
        .first()
    )
    assert m is not None
    assert m.status == "success"


def test_telemetry_isolation_and_unified_contract(tmp_path, db_session: Session):
    """Requirement 4, 6 & 8: Verify telemetry failure does not poison transaction and required contract is 2 files."""
    job = PodcastJob(
        id="job-contract-001",
        gmail_message_id="msg-cnt-1",
        sender_email="user@example.com",
        request_mode=RequestMode.RESEARCH.value,
        source_type=SourceType.EMAIL_BODY.value,
        source_hash="hashcnt1",
        source_text="Sample research source text",
        status=JobState.COMPLETE.value,
        created_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    req_types = get_required_artifact_types(job)
    assert req_types == ["audio", "details"]

    # Verify record_stage_metric with invalid job_id (FK violation) catches exception safely
    record_stage_metric(
        job_id="non-existent-job-xyz",
        stage="DRIVE_AUDIO_UPLOAD",
        started_at=datetime.now(UTC),
        status="success",
    )

    # Business session remains healthy and unpoisoned
    db_session.refresh(job)
    assert job.status == JobState.COMPLETE.value


def test_phase1_performance_endpoints_functional(db_session: Session, monkeypatch):
    """Requirement 8: Verify Phase 1 performance GET endpoints return HTTP 200 with valid metrics payload."""
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")
    from apps.api.main import app

    now = datetime.now(UTC)
    job = PodcastJob(
        id="job-perf-end-001",
        gmail_message_id="msg-pe-1",
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hashpe1",
        source_text="Sample perf source text",
        status=JobState.COMPLETE.value,
        created_at=now - timedelta(seconds=10),
        completed_at=now,
    )
    db_session.add(job)
    db_session.commit()

    m1 = JobProcessingMetric(
        id="m-pe-1",
        job_id=job.id,
        stage="INTAKE_TOTAL",
        started_at=now - timedelta(seconds=10),
        finished_at=now - timedelta(seconds=9),
        duration_ms=1000,
        status="success",
        created_at=now,
    )
    db_session.add(m1)
    db_session.commit()

    client = TestClient(app)

    r_job = client.get(f"/api/v1/jobs/{job.id}/performance")
    assert r_job.status_code == 200
    j_data = r_job.json()
    assert j_data["job_id"] == job.id
    assert "stages" in j_data

    r_ops = client.get("/api/v1/ops/performance")
    assert r_ops.status_code == 200
    o_data = r_ops.json()
    assert "stages" in o_data
