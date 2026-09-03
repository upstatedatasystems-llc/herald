import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.main import app
from herald.config import settings
from herald.db.models import JobState, PodcastJob


@pytest.fixture
def api_client(monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")
    return TestClient(app)


def test_delivery_nudge_endpoint(api_client, db_session: Session):
    job_id = str(uuid.uuid4())
    job = PodcastJob(
        id=job_id,
        gmail_message_id=f"msg-nudge-{job_id[:8]}",
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-nudge-1",
        source_text="Test source text.",
        status=JobState.AUDIO_READY.value,
        created_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    r = api_client.post("/api/v1/delivery/nudge", json={"job_id": job_id, "event": "AUDIO_READY"})
    assert r.status_code == 200
    d = r.json()
    assert d["nudged"] is True
    assert d["job_id"] == job_id


def test_details_finalized_endpoint(api_client, db_session: Session):
    job_id = str(uuid.uuid4())
    job = PodcastJob(
        id=job_id,
        gmail_message_id=f"msg-fin-{job_id[:8]}",
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-fin-1",
        source_text="Test source text.",
        status=JobState.COMPLETE.value,
        completed_at=datetime.now(UTC),
        details_drive_file_id="drive-details-123",
        created_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    r = api_client.post(
        f"/api/v1/jobs/{job_id}/details-finalized",
        json={
            "details_drive_file_id": "drive-details-123",
            "details_drive_web_link": "https://drive.google.com/file/d/drive-details-123/view",
        },
    )
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == JobState.COMPLETE.value
    assert d["details_finalized_at"] is not None

    db_session.refresh(job)
    assert job.details_finalized_at is not None


def test_duplicate_nudge_safety(api_client, db_session: Session):
    """Nudging an already completed job returns HTTP 200 safely without altering job state."""
    job_id = str(uuid.uuid4())
    job = PodcastJob(
        id=job_id,
        gmail_message_id=f"msg-dup-nudge-{job_id[:8]}",
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-dup-nudge-1",
        source_text="Test source text.",
        status=JobState.COMPLETE.value,
        completed_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    # Nudge completed job
    r = api_client.post("/api/v1/delivery/nudge", json={"job_id": job_id, "event": "AUDIO_READY"})
    assert r.status_code == 200
    assert r.json()["nudged"] is True

    # Claim delivery call returns claimed: False cleanly
    r_claim = api_client.post("/api/v1/delivery/claim")
    assert r_claim.status_code == 200
    assert r_claim.json()["claimed"] is False


def test_details_finalization_retry_safety(api_client, db_session: Session):
    """Retrying details-finalized endpoint updates existing record safely without duplicate artifacts."""
    job_id = str(uuid.uuid4())
    job = PodcastJob(
        id=job_id,
        gmail_message_id=f"msg-retry-fin-{job_id[:8]}",
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-retry-fin-1",
        source_text="Test source text.",
        status=JobState.COMPLETE.value,
        completed_at=datetime.now(UTC),
        details_drive_file_id="drive-details-orig",
        created_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    r1 = api_client.post(
        f"/api/v1/jobs/{job_id}/details-finalized",
        json={"details_drive_file_id": "drive-details-orig"},
    )
    assert r1.status_code == 200

    # Retry with updated file ID
    r2 = api_client.post(
        f"/api/v1/jobs/{job_id}/details-finalized",
        json={"details_drive_file_id": "drive-details-updated"},
    )
    assert r2.status_code == 200
    assert r2.json()["details_drive_file_id"] == "drive-details-updated"

    db_session.refresh(job)
    assert job.details_drive_file_id == "drive-details-updated"
