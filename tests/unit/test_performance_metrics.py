from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app, normalize_gmail_timestamp
from herald.audio.artifact_generator import ensure_details_artifact
from herald.config import settings
from herald.db.models import JobProcessingMetric, JobState, PodcastJob, SourceType
from herald.services.performance_metrics import record_stage_metric

client = TestClient(app)
AUTH_HEADERS = {"X-API-Key": "test-api-key"}


def test_gmail_timestamp_normalization():
    """Verify normalize_gmail_timestamp handles epoch ms, RFC 2822, ISO-8601, and invalid null fallback."""
    # Epoch ms integer
    dt_epoch = normalize_gmail_timestamp(1770480000000)
    assert dt_epoch is not None
    assert dt_epoch.tzinfo == UTC

    # Epoch ms string
    dt_str_epoch = normalize_gmail_timestamp("1770480000000")
    assert dt_str_epoch is not None

    # RFC 2822 mail header date
    dt_rfc = normalize_gmail_timestamp("Fri, 07 Aug 2026 14:30:00 -0400")
    assert dt_rfc is not None
    assert dt_rfc.tzinfo == UTC

    # ISO-8601 UTC
    dt_iso = normalize_gmail_timestamp("2026-08-07T18:30:00Z")
    assert dt_iso is not None
    assert dt_iso.tzinfo == UTC

    # Invalid / None returns None without failing
    assert normalize_gmail_timestamp(None) is None
    assert normalize_gmail_timestamp("") is None
    assert normalize_gmail_timestamp("not-a-valid-date") is None


def test_metrics_db_write_isolation(db_session):
    """
    Requirement 1: Metrics DB writes MUST be isolated from business transactions.
    Verify that if SessionLocal raises an exception during metric writing,
    the exception is caught, logged, and the caller's session/business transaction is NOT poisoned.
    """
    job = PodcastJob(
        id="job-metrics-iso-001",
        gmail_message_id="msg-iso-1",
        sender_email="auth@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="isohash1",
        source_text="Test source text",
        status=JobState.RECEIVED.value,
        created_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    with patch("herald.services.performance_metrics.SessionLocal") as mock_session_factory:
        mock_session = MagicMock()
        mock_session.commit.side_effect = RuntimeError("Simulated Database Error")
        mock_session_factory.return_value = mock_session

        # Record metric should swallow exception and return None without raising
        result = record_stage_metric(
            job_id=job.id,
            stage="INTAKE_TOTAL",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            duration_ms=150,
            status="success",
        )
        assert result is None

    # Verify caller's main business session is still active and valid
    job.status = JobState.VALIDATING.value
    db_session.commit()
    db_session.refresh(job)
    assert job.status == JobState.VALIDATING.value


def test_weighted_kokoro_rtf_calculation(db_session, monkeypatch):
    """
    Requirement 7: episode Kokoro RTF = sum(successful wall time) / sum(successful audio duration).
    Verify that RTF is calculated weighted by duration rather than an unweighted mean.
    """
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")

    job_id = "job-rtf-calc-001"
    job = PodcastJob(
        id=job_id,
        gmail_message_id="msg-rtf-1",
        sender_email="auth@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="rtfhash1",
        source_text="Source text",
        status=JobState.COMPLETE.value,
        created_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    now = datetime.now(UTC)

    # Chunk 1: wall_time = 2000ms, audio_duration = 10000ms -> RTF = 0.20
    m1 = JobProcessingMetric(
        id="m1",
        job_id=job_id,
        stage="KOKORO_REQUEST",
        sequence_index=1,
        attempt=1,
        started_at=now,
        finished_at=now + timedelta(seconds=2),
        duration_ms=2000,
        status="success",
        audio_duration_ms=10000,
        created_at=now,
    )
    # Chunk 2: wall_time = 8000ms, audio_duration = 10000ms -> RTF = 0.80
    m2 = JobProcessingMetric(
        id="m2",
        job_id=job_id,
        stage="KOKORO_REQUEST",
        sequence_index=2,
        attempt=1,
        started_at=now,
        finished_at=now + timedelta(seconds=8),
        duration_ms=8000,
        status="success",
        audio_duration_ms=10000,
        created_at=now,
    )
    # Failed attempt (must be excluded from RTF sum)
    m3 = JobProcessingMetric(
        id="m3",
        job_id=job_id,
        stage="KOKORO_REQUEST",
        sequence_index=3,
        attempt=1,
        started_at=now,
        finished_at=now + timedelta(seconds=5),
        duration_ms=5000,
        status="failed",
        audio_duration_ms=0,
        created_at=now,
    )

    db_session.add_all([m1, m2, m3])
    db_session.commit()

    res = client.get(f"/api/v1/jobs/{job_id}/performance", headers=AUTH_HEADERS)
    assert res.status_code == 200
    perf = res.json()

    # Sum wall time = 10,000ms, Sum audio duration = 20,000ms -> Weighted RTF = 10000 / 20000 = 0.5
    assert perf["kokoro"]["rtf"] == 0.5
    assert perf["kokoro"]["requests"] == 3
    assert perf["kokoro"]["successful_requests"] == 2
    assert perf["kokoro"]["failed_attempts"] == 1


def test_job_and_ops_performance_endpoints(db_session, monkeypatch):
    """Verify /jobs/{job_id}/performance and /ops/performance return structured schemas."""
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")

    job_id = "job-ops-perf-001"
    job = PodcastJob(
        id=job_id,
        gmail_message_id="msg-ops-1",
        sender_email="auth@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="opshash1",
        source_text="Source text",
        status=JobState.COMPLETE.value,
        created_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    now = datetime.now(UTC)
    m = JobProcessingMetric(
        id="m-intake-1",
        job_id=job_id,
        stage="INTAKE_TOTAL",
        started_at=now,
        finished_at=now + timedelta(milliseconds=120),
        duration_ms=120,
        status="success",
        created_at=now,
    )
    db_session.add(m)
    db_session.commit()

    res_job = client.get(f"/api/v1/jobs/{job_id}/performance", headers=AUTH_HEADERS)
    assert res_job.status_code == 200
    data_job = res_job.json()
    assert data_job["job_id"] == job_id
    assert "stages" in data_job
    assert data_job["stages"]["INTAKE_TOTAL"] == 120

    res_ops = client.get("/api/v1/ops/performance?limit=10", headers=AUTH_HEADERS)
    assert res_ops.status_code == 200
    data_ops = res_ops.json()
    assert "stages" in data_ops
    assert "INTAKE_TOTAL" in data_ops["stages"]
    assert data_ops["stages"]["INTAKE_TOTAL"]["mean"] == 120.0


def test_details_artifact_includes_audio_upload_metrics(tmp_path, db_session):
    """
    Requirement 6: details.md includes DRIVE_AUDIO_UPLOAD metric after audio upload complete.
    """
    now = datetime.now(UTC)
    job = PodcastJob(
        id="job-details-metric-001",
        gmail_message_id="msg-dtl-1",
        sender_email="auth@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="dtlhash1",
        source_text="Detailed source text paragraph.",
        status=JobState.UPLOADING.value,
        drive_file_id="drive-audio-id-999",
        drive_web_link="https://drive.google.com/file/d/drive-audio-id-999",
        created_at=now,
        audio_ready_at=now,
        script_json={
            "episode_title": "Details Metric Test",
            "segments": [{"order": 1, "heading": "Intro", "narration": "Intro text"}],
        },
    )
    db_session.add(job)
    db_session.commit()

    metric = JobProcessingMetric(
        id="m-audio-up-1",
        job_id=job.id,
        stage="DRIVE_AUDIO_UPLOAD",
        started_at=now,
        finished_at=now + timedelta(seconds=3),
        duration_ms=3000,
        status="success",
        output_bytes=5000000,
        created_at=now,
    )
    db_session.add(metric)
    db_session.commit()
    db_session.refresh(job)

    path = ensure_details_artifact(job, tmp_path, db=db_session)
    assert path.exists()

    content = path.read_text(encoding="utf-8")
    assert "Drive Audio Upload" in content
    assert "3000 ms" in content or "3.00s" in content or "3000" in content
