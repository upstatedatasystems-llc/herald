"""
Unit tests for Herald terminal diagnostics archive generation, session isolation,
concurrent safety, retention cleanup, and non-terminal active job protection.
"""

import json
import os
import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from herald.db.models import JobState, PodcastJob
from herald.db.state_machine import transition_job_state
from herald.services.diagnostics_export import (
    cleanup_expired_diagnostics_archives,
    ensure_terminal_diagnostics_archive,
    get_diagnostics_base_dir,
    get_terminal_diagnostics_path,
    sweep_unarchived_terminal_jobs,
)


@pytest.fixture
def clean_diagnostics_dir(tmp_path: Path, monkeypatch):
    """Point HERALD_LOG_DIR to a clean temporary directory for isolated testing."""
    diag_dir = tmp_path / "logs" / "diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("herald.config.settings.HERALD_LOG_DIR", str(tmp_path / "logs"))
    return diag_dir


def _create_dummy_job(db: Session, status: str = JobState.QUEUED_TTS.value) -> PodcastJob:
    job = PodcastJob(
        request_mode="standard",
        source_type="text",
        source_hash="dummy_source_hash_12345",
        source_text="Test source content for podcast diagnostics testing.",
        custom_title="Diagnostics Test Episode",
        status=status,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_ensure_terminal_diagnostics_archive_complete(clean_diagnostics_dir, db_session: Session):
    job = _create_dummy_job(db_session, status=JobState.AUDIO_READY.value)
    transition_job_state(db_session, job, JobState.COMPLETE.value, component="test")
    res = ensure_terminal_diagnostics_archive(job.id, JobState.COMPLETE.value)
    assert res is not None

    canonical_path = get_terminal_diagnostics_path(job.id, JobState.COMPLETE.value)
    assert canonical_path.name == f"{job.id}_COMPLETE.zip"
    assert canonical_path.exists()
    assert canonical_path.stat().st_size > 0

    with zipfile.ZipFile(canonical_path, "r") as zf:
        namelist = zf.namelist()
        assert "manifest.json" in namelist
        assert "source.txt" in namelist
        assert "state-transitions.json" in namelist


def test_ensure_terminal_diagnostics_archive_failed_final(clean_diagnostics_dir, db_session: Session):
    job = _create_dummy_job(db_session, status=JobState.QUEUED_TTS.value)
    transition_job_state(
        db_session,
        job,
        JobState.FAILED_FINAL.value,
        component="test",
        message="Fatal synthesis error",
        error_category="TTS_CRASH",
    )
    res = ensure_terminal_diagnostics_archive(job.id, JobState.FAILED_FINAL.value)
    assert res is not None

    canonical_path = get_terminal_diagnostics_path(job.id, JobState.FAILED_FINAL.value)
    assert canonical_path.name == f"{job.id}_FAILED_FINAL.zip"
    assert canonical_path.exists()
    assert canonical_path.stat().st_size > 0


def test_ensure_terminal_diagnostics_archive_cancelled(clean_diagnostics_dir, db_session: Session):
    job = _create_dummy_job(db_session, status=JobState.AWAITING_APPROVAL.value)
    transition_job_state(
        db_session,
        job,
        JobState.CANCELLED.value,
        component="telegram-approval",
        message="Cancelled by user",
    )
    res = ensure_terminal_diagnostics_archive(job.id, JobState.CANCELLED.value)
    assert res is not None

    canonical_path = get_terminal_diagnostics_path(job.id, JobState.CANCELLED.value)
    assert canonical_path.name == f"{job.id}_CANCELLED.zip"
    assert canonical_path.exists()


def test_diagnostics_session_isolation(clean_diagnostics_dir, db_session: Session):
    """Verify diagnostics generation uses its own isolated session and export errors do not poison caller."""
    job = _create_dummy_job(db_session, status=JobState.COMPLETE.value)

    # Simulate export failure inside ensure_terminal_diagnostics_archive
    with patch(
        "herald.services.diagnostics_export.generate_job_diagnostics_zip",
        side_effect=RuntimeError("Simulated export error"),
    ):
        result = ensure_terminal_diagnostics_archive(job.id, JobState.COMPLETE.value)
        assert result is None

    # Verify caller db_session is completely intact and healthy
    refreshed_job = db_session.query(PodcastJob).filter(PodcastJob.id == job.id).first()
    assert refreshed_job is not None
    assert refreshed_job.status == JobState.COMPLETE.value


def test_concurrent_generation_safety_atomic_replace(clean_diagnostics_dir, db_session: Session):
    """Verify unique temp staging file is used and atomically replaces target path."""
    job = _create_dummy_job(db_session, status=JobState.COMPLETE.value)

    target_path = get_terminal_diagnostics_path(job.id, JobState.COMPLETE.value)
    res_path = ensure_terminal_diagnostics_archive(job.id, JobState.COMPLETE.value)
    assert res_path == target_path
    assert target_path.exists()

    # Second call is idempotent and detects existing archive
    res_path_2 = ensure_terminal_diagnostics_archive(job.id, JobState.COMPLETE.value)
    assert res_path_2 == target_path


def test_active_job_diagnostics_non_terminal_preservation(clean_diagnostics_dir, db_session: Session):
    """Verify non-terminal active job does not create canonical archive and respects temporary handling."""
    job = _create_dummy_job(db_session, status=JobState.SYNTHESIZING.value)

    # ensure_terminal_diagnostics_archive skips non-terminal jobs
    res = ensure_terminal_diagnostics_archive(job.id, job.status)
    assert res is None

    canonical_path = get_terminal_diagnostics_path(job.id, job.status)
    assert not canonical_path.exists()


def test_cleanup_expired_diagnostics_archives(clean_diagnostics_dir):
    """Verify cleanup deletes archives older than retention threshold and stale temp files."""
    base_dir = clean_diagnostics_dir

    now = time.time()
    # 1. Fresh archive (1 day old)
    fresh_file = base_dir / "job1_COMPLETE.zip"
    fresh_file.write_bytes(b"PK fresh")
    os.utime(fresh_file, (now - 86400, now - 86400))

    # 2. Expired archive (35 days old)
    expired_file = base_dir / "job2_FAILED_FINAL.zip"
    expired_file.write_bytes(b"PK expired")
    os.utime(expired_file, (now - 35 * 86400, now - 35 * 86400))

    # 3. Fresh temp file (5 mins old)
    fresh_tmp = base_dir / "job1_COMPLETE.zip.tmp.1234.5678"
    fresh_tmp.write_bytes(b"PK tmp")
    os.utime(fresh_tmp, (now - 300, now - 300))

    # 4. Abandoned stale temp file (2 hours old)
    stale_tmp = base_dir / "job2_COMPLETE.zip.tmp.9999.0000"
    stale_tmp.write_bytes(b"PK stale tmp")
    os.utime(stale_tmp, (now - 7200, now - 7200))

    deleted_count = cleanup_expired_diagnostics_archives(retention_days=30)
    assert deleted_count == 1
    assert fresh_file.exists()
    assert not expired_file.exists()
    assert fresh_tmp.exists()
    assert not stale_tmp.exists()


def test_sweep_unarchived_terminal_jobs(clean_diagnostics_dir, db_session: Session):
    """Verify sweep detects recent terminal jobs by terminal timestamp (completed_at/updated_at/created_at)."""
    now = datetime.now(UTC)
    # job1: created recently, terminal recently
    job1 = _create_dummy_job(db_session, status=JobState.COMPLETE.value)
    job1.completed_at = now - timedelta(hours=1)

    # job2: created 45 days ago, but completed 1 day ago (should be swept via completed_at coalesce)
    job2 = _create_dummy_job(db_session, status=JobState.FAILED_FINAL.value)
    job2.created_at = now - timedelta(days=45)
    job2.updated_at = now - timedelta(days=45)
    job2.completed_at = now - timedelta(days=1)

    # job3: created 45 days ago, completed 45 days ago (should NOT be swept, older than 30 days)
    job3 = _create_dummy_job(db_session, status=JobState.COMPLETE.value)
    job3.created_at = now - timedelta(days=45)
    job3.updated_at = now - timedelta(days=45)
    job3.completed_at = now - timedelta(days=45)
    db_session.commit()

    # Ensure no archives exist initially
    arc1 = get_terminal_diagnostics_path(job1.id, job1.status)
    arc2 = get_terminal_diagnostics_path(job2.id, job2.status)
    arc3 = get_terminal_diagnostics_path(job3.id, job3.status)
    assert not arc1.exists()
    assert not arc2.exists()
    assert not arc3.exists()

    swept_count = sweep_unarchived_terminal_jobs(db_session, max_age_days=30, limit=10)
    assert swept_count == 2
    assert arc1.exists()
    assert arc2.exists()
    assert not arc3.exists()

    # Second sweep generates 0 because archives already exist
    swept_count_2 = sweep_unarchived_terminal_jobs(db_session, max_age_days=30, limit=10)
    assert swept_count_2 == 0


def test_pipeline_extraction_failure_generates_archive(clean_diagnostics_dir, db_session: Session):
    """Verify URL extraction failure in pipeline generates FAILED_FINAL archive."""
    from herald.core.models import HeraldRequest
    from herald.core.pipeline import process_herald_request

    req = HeraldRequest(source_url="http://invalid-nonexistent-domain.test/article", request_mode="literal")
    resp = process_herald_request(db=db_session, req=req)
    assert resp.status == JobState.FAILED_FINAL.value
    assert resp.job_id

    canonical_path = get_terminal_diagnostics_path(resp.job_id, JobState.FAILED_FINAL.value)
    assert canonical_path.exists()
    assert canonical_path.stat().st_size > 0


def test_deliver_job_diagnostics_preserves_terminal_archive(clean_diagnostics_dir, db_session: Session):
    """Verify deliver_job_diagnostics serves canonical archive for terminal job without unlinking."""
    from unittest.mock import MagicMock
    from herald.telegram.delivery import deliver_job_diagnostics

    job = _create_dummy_job(db_session, status=JobState.COMPLETE.value)
    canonical_path = ensure_terminal_diagnostics_archive(job.id, JobState.COMPLETE.value)
    assert canonical_path.exists()

    mock_client = MagicMock()
    success = deliver_job_diagnostics(
        db=db_session,
        client=mock_client,
        job=job,
        chat_id=12345,
    )
    assert success is True
    assert mock_client.send_document.called
    # Crucial assertion: terminal canonical archive must NOT be deleted in finally
    assert canonical_path.exists()


def test_ensure_terminal_diagnostics_archive_authoritative_db_status_rejects_non_terminal(
    clean_diagnostics_dir, db_session: Session
):
    """Regression test: caller passes expected_status='COMPLETE', but DB row status is DELIVERING.
    Must return None and NOT generate any archive file."""
    job = _create_dummy_job(db_session, status=JobState.DELIVERING.value)

    result = ensure_terminal_diagnostics_archive(job.id, expected_status=JobState.COMPLETE.value)
    assert result is None

    assert not get_terminal_diagnostics_path(job.id, JobState.COMPLETE.value).exists()
    assert not get_terminal_diagnostics_path(job.id, JobState.DELIVERING.value).exists()


def test_concurrent_terminal_diagnostics_archive_generation(clean_diagnostics_dir, db_session: Session):
    """Verify 2 concurrent threads generating an archive for the same job both succeed,
    yielding a valid non-corrupt canonical ZIP with no lingering temp files."""
    job = _create_dummy_job(db_session, status=JobState.COMPLETE.value)

    results = []
    errors = []

    def target():
        try:
            res = ensure_terminal_diagnostics_archive(job.id, JobState.COMPLETE.value)
            results.append(res)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=target)
    t2 = threading.Thread(target=target)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    assert len(results) == 2
    canonical_path = get_terminal_diagnostics_path(job.id, JobState.COMPLETE.value)
    assert canonical_path.exists()
    assert canonical_path.stat().st_size > 0

    with zipfile.ZipFile(canonical_path, "r") as zf:
        namelist = zf.namelist()
        assert "manifest.json" in namelist

    tmp_files = [f for f in clean_diagnostics_dir.iterdir() if ".tmp." in f.name]
    assert tmp_files == []


def test_delivery_telemetry_persisted_in_complete_archive(clean_diagnostics_dir, db_session: Session):
    """Regression test: delivery path commits TELEGRAM_DELIVERY stage metric and
    TELEGRAM_DELIVERY_COMPLETE diagnostic event before ensure_terminal_diagnostics_archive,
    proving final persisted COMPLETE ZIP contains all delivery telemetry."""
    from unittest.mock import MagicMock
    from herald.telegram.delivery import deliver_single_job

    job = _create_dummy_job(db_session, status=JobState.AUDIO_READY.value)
    mock_client = MagicMock()
    mock_client.send_audio.return_value = {"message_id": 98765}

    audio_path = clean_diagnostics_dir.parent / "test_audio.mp3"
    audio_path.write_bytes(b"ID3FakeAudioStreamBytes")
    job.telegram_chat_id = 123456
    job.local_audio_path = str(audio_path)
    job.audio_duration_seconds = 120.0
    db_session.commit()

    success = deliver_single_job(
        db=db_session,
        job=job,
        client=mock_client,
    )
    assert success is True

    canonical_path = get_terminal_diagnostics_path(job.id, JobState.COMPLETE.value)
    assert canonical_path.exists()

    with zipfile.ZipFile(canonical_path, "r") as zf:
        namelist = zf.namelist()
        assert "manifest.json" in namelist
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["job_id"] == job.id
        assert manifest["status"] == JobState.COMPLETE.value

        assert "state-transitions.json" in namelist
        transitions = json.loads(zf.read("state-transitions.json").decode("utf-8"))
        assert any(t["to_state"] == JobState.COMPLETE.value for t in transitions)

        assert "processing-metrics.json" in namelist
        metrics = json.loads(zf.read("processing-metrics.json").decode("utf-8"))
        delivery_metric = next((m for m in metrics if m["stage"] == "TELEGRAM_DELIVERY"), None)
        assert delivery_metric is not None
        assert delivery_metric["status"] == "success"
        assert delivery_metric["output_bytes"] > 0
        assert delivery_metric["duration_ms"] is not None

        assert "diagnostic-events.jsonl" in namelist
        event_lines = [json.loads(line) for line in zf.read("diagnostic-events.jsonl").decode("utf-8").splitlines() if line.strip()]
        delivery_event = next((e for e in event_lines if e["event_type"] == "TELEGRAM_DELIVERY_COMPLETE"), None)
        assert delivery_event is not None


def test_delivery_permanent_failure_persisted_in_failed_final_archive(clean_diagnostics_dir, db_session: Session):
    """Verify permanent delivery failure (3 attempts) creates FAILED_FINAL archive with TELEGRAM_DELIVERY_FAILED event."""
    from unittest.mock import MagicMock
    from herald.telegram.client import TelegramAPIError
    from herald.telegram.delivery import deliver_single_job

    job = _create_dummy_job(db_session, status=JobState.AUDIO_READY.value)
    job.delivery_attempt_count = 2  # next attempt will be 3rd -> permanent failure
    mock_client = MagicMock()
    mock_client.send_audio.side_effect = TelegramAPIError("Telegram Network Timeout")
    mock_client.send_document.side_effect = TelegramAPIError("Telegram Network Timeout")

    audio_path = clean_diagnostics_dir.parent / "test_audio_fail.mp3"
    audio_path.write_bytes(b"ID3FakeAudioStreamBytes")
    job.telegram_chat_id = 123456
    job.local_audio_path = str(audio_path)
    db_session.commit()

    success = deliver_single_job(
        db=db_session,
        job=job,
        client=mock_client,
    )
    assert success is False

    canonical_path = get_terminal_diagnostics_path(job.id, JobState.FAILED_FINAL.value)
    assert canonical_path.exists()

    with zipfile.ZipFile(canonical_path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["status"] == JobState.FAILED_FINAL.value

        event_lines = [json.loads(line) for line in zf.read("diagnostic-events.jsonl").decode("utf-8").splitlines() if line.strip()]
        assert any(e["event_type"] == "TELEGRAM_DELIVERY_FAILED" for e in event_lines)

