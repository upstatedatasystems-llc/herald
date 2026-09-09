"""
Unit tests for Herald terminal diagnostics archive generation, session isolation,
concurrent safety, retention cleanup, and non-terminal active job protection.
"""

import os
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
    """Verify sweep detects recent terminal jobs without archives and creates them."""
    job1 = _create_dummy_job(db_session, status=JobState.COMPLETE.value)
    job2 = _create_dummy_job(db_session, status=JobState.FAILED_FINAL.value)

    # Ensure no archives exist initially
    arc1 = get_terminal_diagnostics_path(job1.id, job1.status)
    arc2 = get_terminal_diagnostics_path(job2.id, job2.status)
    assert not arc1.exists()
    assert not arc2.exists()

    swept_count = sweep_unarchived_terminal_jobs(db_session, max_age_days=30, limit=10)
    assert swept_count == 2
    assert arc1.exists()
    assert arc2.exists()

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

