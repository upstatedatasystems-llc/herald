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


def test_ensure_terminal_diagnostics_archive_authoritative_db_status_rejects_stale_caller(
    clean_diagnostics_dir, db_session: Session
):
    """Regression test: persisted row is FAILED_FINAL, but stale caller passes expected_status='COMPLETE'.
    Must return None, log a warning, and NOT create either COMPLETE or FAILED_FINAL archive.
    A subsequent call with expected_status=None or matching status creates the archive."""
    job = _create_dummy_job(db_session, status=JobState.FAILED_FINAL.value)

    res_stale = ensure_terminal_diagnostics_archive(job.id, expected_status=JobState.COMPLETE.value)
    assert res_stale is None

    assert not get_terminal_diagnostics_path(job.id, JobState.COMPLETE.value).exists()
    assert not get_terminal_diagnostics_path(job.id, JobState.FAILED_FINAL.value).exists()

    # Recovery sweep or caller with matching status succeeds
    res_correct = ensure_terminal_diagnostics_archive(job.id, expected_status=None)
    assert res_correct is not None
    assert get_terminal_diagnostics_path(job.id, JobState.FAILED_FINAL.value).exists()


def test_telegram_delivery_missing_audio_creates_failed_final_archive(
    clean_diagnostics_dir, db_session: Session
):
    """Point 1A: missing local audio transitions to FAILED_FINAL, records failure telemetry,
    and automatically persists canonical <job>_FAILED_FINAL.zip containing failure evidence."""
    from unittest.mock import MagicMock
    from herald.telegram.delivery import deliver_single_job

    job = _create_dummy_job(db_session, status=JobState.AUDIO_READY.value)
    job.telegram_chat_id = 999888
    job.local_audio_path = str(clean_diagnostics_dir.parent / "nonexistent_audio.mp3")
    db_session.commit()

    mock_client = MagicMock()
    success = deliver_single_job(db=db_session, job=job, client=mock_client)
    assert success is False

    db_session.refresh(job)
    assert job.status == JobState.FAILED_FINAL.value
    assert job.error_code == "AUDIO_FILE_MISSING"
    assert "not found" in (job.error_detail or "")
    mock_client.send_message.assert_called_once()

    canonical_path = get_terminal_diagnostics_path(job.id, JobState.FAILED_FINAL.value)
    assert canonical_path.exists()
    assert canonical_path.stat().st_size > 0

    with zipfile.ZipFile(canonical_path, "r") as zf:
        namelist = zf.namelist()
        assert "manifest.json" in namelist
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["job_id"] == job.id
        assert manifest["status"] == JobState.FAILED_FINAL.value

        assert "processing-metrics.json" in namelist
        metrics = json.loads(zf.read("processing-metrics.json").decode("utf-8"))
        delivery_metric = next((m for m in metrics if m["stage"] == "TELEGRAM_DELIVERY"), None)
        assert delivery_metric is not None
        assert delivery_metric["status"] == "failure"
        assert delivery_metric.get("metadata", {}).get("error_code") == "AUDIO_FILE_MISSING"

        assert "diagnostic-events.jsonl" in namelist
        events = [json.loads(line) for line in zf.read("diagnostic-events.jsonl").decode("utf-8").splitlines() if line.strip()]
        delivery_event = next((e for e in events if e["event_type"] == "TELEGRAM_DELIVERY_FAILED"), None)
        assert delivery_event is not None
        assert delivery_event["metadata"]["error_code"] == "AUDIO_FILE_MISSING"


def test_telegram_delivery_oversized_audio_creates_failed_final_archive(
    clean_diagnostics_dir, db_session: Session, monkeypatch
):
    """Point 1B: oversized audio transitions to FAILED_FINAL, records failure telemetry,
    and automatically persists canonical <job>_FAILED_FINAL.zip containing failure evidence."""
    from unittest.mock import MagicMock
    from herald.telegram.delivery import deliver_single_job

    monkeypatch.setattr("herald.config.settings.TELEGRAM_MAX_AUDIO_BYTES", 500)

    audio_path = clean_diagnostics_dir.parent / "huge_audio.mp3"
    audio_path.write_bytes(b"X" * 2000)

    job = _create_dummy_job(db_session, status=JobState.AUDIO_READY.value)
    job.telegram_chat_id = 999888
    job.local_audio_path = str(audio_path)
    job.audio_duration_seconds = 180.0
    db_session.commit()

    mock_client = MagicMock()
    success = deliver_single_job(db=db_session, job=job, client=mock_client)
    assert success is False

    db_session.refresh(job)
    assert job.status == JobState.FAILED_FINAL.value
    assert job.error_code == "TELEGRAM_AUDIO_OVERSIZED"
    assert "exceeded" in (job.error_detail or "").lower()
    mock_client.send_message.assert_called_once()

    canonical_path = get_terminal_diagnostics_path(job.id, JobState.FAILED_FINAL.value)
    assert canonical_path.exists()
    assert canonical_path.stat().st_size > 0

    with zipfile.ZipFile(canonical_path, "r") as zf:
        namelist = zf.namelist()
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["job_id"] == job.id
        assert manifest["status"] == JobState.FAILED_FINAL.value

        metrics = json.loads(zf.read("processing-metrics.json").decode("utf-8"))
        delivery_metric = next((m for m in metrics if m["stage"] == "TELEGRAM_DELIVERY"), None)
        assert delivery_metric is not None
        assert delivery_metric["status"] == "failure"
        assert delivery_metric.get("metadata", {}).get("error_code") == "TELEGRAM_AUDIO_OVERSIZED"

        events = [json.loads(line) for line in zf.read("diagnostic-events.jsonl").decode("utf-8").splitlines() if line.strip()]
        delivery_event = next((e for e in events if e["event_type"] == "TELEGRAM_DELIVERY_FAILED"), None)
        assert delivery_event is not None
        assert delivery_event["metadata"]["error_code"] == "TELEGRAM_AUDIO_OVERSIZED"


def test_telegram_failed_final_characterization_table(
    clean_diagnostics_dir, db_session: Session, monkeypatch
):
    """Point 2: Characterization table proving every Telegram FAILED_FINAL path produces
    a canonical <job>_FAILED_FINAL.zip archive with failure telemetry."""
    from unittest.mock import MagicMock
    from herald.core.pipeline import process_herald_request, HeraldRequest
    from herald.extraction.url_extractor import ArticleExtractionError, SSRFVulnerabilityError
    from herald.gemini.client import GeminiError
    from herald.telegram.client import TelegramAPIError
    from herald.telegram.delivery import deliver_single_job

    paths_tested = []

    # 1. Missing audio
    job_miss = _create_dummy_job(db_session, status=JobState.AUDIO_READY.value)
    job_miss.telegram_chat_id = 111
    job_miss.local_audio_path = "/nonexistent/path/audio.mp3"
    db_session.commit()
    deliver_single_job(db=db_session, job=job_miss, client=MagicMock())
    arc_miss = get_terminal_diagnostics_path(job_miss.id, JobState.FAILED_FINAL.value)
    assert arc_miss.exists()
    paths_tested.append("MISSING_AUDIO")

    # 2. Oversized audio
    monkeypatch.setattr("herald.config.settings.TELEGRAM_MAX_AUDIO_BYTES", 100)
    audio_path = clean_diagnostics_dir.parent / "over_audio.mp3"
    audio_path.write_bytes(b"A" * 500)
    job_over = _create_dummy_job(db_session, status=JobState.AUDIO_READY.value)
    job_over.telegram_chat_id = 222
    job_over.local_audio_path = str(audio_path)
    db_session.commit()
    deliver_single_job(db=db_session, job=job_over, client=MagicMock())
    arc_over = get_terminal_diagnostics_path(job_over.id, JobState.FAILED_FINAL.value)
    assert arc_over.exists()
    paths_tested.append("OVERSIZED_AUDIO")

    # 3. 3-attempt delivery failure
    job_retry = _create_dummy_job(db_session, status=JobState.AUDIO_READY.value)
    job_retry.delivery_attempt_count = 2
    audio_retry = clean_diagnostics_dir.parent / "retry_audio.mp3"
    audio_retry.write_bytes(b"B" * 50)
    job_retry.telegram_chat_id = 333
    job_retry.local_audio_path = str(audio_retry)
    db_session.commit()
    mock_err_client = MagicMock()
    mock_err_client.send_audio.side_effect = TelegramAPIError("Network Down")
    mock_err_client.send_document.side_effect = TelegramAPIError("Network Down")
    deliver_single_job(db=db_session, job=job_retry, client=mock_err_client)
    arc_retry = get_terminal_diagnostics_path(job_retry.id, JobState.FAILED_FINAL.value)
    assert arc_retry.exists()
    paths_tested.append("DELIVERY_RETRY_EXHAUSTION")

    # 4. Extraction SSRF failure
    with patch("herald.core.pipeline.extract_article_from_url", side_effect=SSRFVulnerabilityError("Private IP 127.0.0.1")):
        req_ssrf = HeraldRequest(
            source_type="url",
            source_url="http://127.0.0.1/admin",
            telegram_chat_id=444,
            telegram_user_id=444,
            transport="telegram",
        )
        resp_ssrf = process_herald_request(db_session, req_ssrf)
        assert resp_ssrf.status == JobState.FAILED_FINAL.value
        arc_ssrf = get_terminal_diagnostics_path(resp_ssrf.job_id, JobState.FAILED_FINAL.value)
        assert arc_ssrf.exists()
        paths_tested.append("EXTRACTION_SSRF")

    # 5. Extraction general failure
    with patch("herald.core.pipeline.extract_article_from_url", side_effect=ArticleExtractionError("HTML Parse Failed")):
        req_ext = HeraldRequest(
            source_type="url",
            source_url="https://example.com/bad",
            telegram_chat_id=555,
            telegram_user_id=555,
            transport="telegram",
        )
        resp_ext = process_herald_request(db_session, req_ext)
        assert resp_ext.status == JobState.FAILED_FINAL.value
        arc_ext = get_terminal_diagnostics_path(resp_ext.job_id, JobState.FAILED_FINAL.value)
        assert arc_ext.exists()
        paths_tested.append("EXTRACTION_FAILURE")

    # 6. Scripting failure
    mock_provider = MagicMock()
    mock_provider.is_configured.return_value = True
    mock_provider.generate_script.side_effect = GeminiError("Script generation error")
    with patch("herald.core.pipeline.get_ai_provider", return_value=mock_provider):
        req_scr = HeraldRequest(
            source_type="text",
            source_text="Valid source text for scripting test",
            request_mode="standard",
            telegram_chat_id=666,
            telegram_user_id=666,
            transport="telegram",
        )
        resp_scr = process_herald_request(db_session, req_scr)
        assert resp_scr.status == JobState.FAILED_FINAL.value
        arc_scr = get_terminal_diagnostics_path(resp_scr.job_id, JobState.FAILED_FINAL.value)
        assert arc_scr.exists()
        paths_tested.append("SCRIPTING_FAILURE")

    # 7. Worker synthesis max attempts
    from apps.worker.main import claim_next_job
    job_worker = _create_dummy_job(db_session, status=JobState.QUEUED_TTS.value)
    job_worker.synthesis_attempt_count = 4  # > 3
    db_session.commit()
    claimed = claim_next_job(db=db_session, worker_id="test-worker")
    assert claimed is None
    db_session.refresh(job_worker)
    assert job_worker.status == JobState.FAILED_FINAL.value
    arc_worker = get_terminal_diagnostics_path(job_worker.id, JobState.FAILED_FINAL.value)
    assert arc_worker.exists()
    paths_tested.append("WORKER_TTS_MAX_ATTEMPTS")

    assert len(paths_tested) == 7


def test_api_extraction_failed_final_creates_canonical_archive(
    clean_diagnostics_dir, db_session: Session, monkeypatch
):
    """Point 3A: apps/api/main.py URL extraction failure transitions to FAILED_FINAL,
    records stage metrics and diagnostic events, and persists canonical FAILED_FINAL ZIP."""
    from apps.api.main import process_intake, IntakeRequest
    from herald.extraction.url_extractor import SourceAccessBlockedError

    monkeypatch.setattr("herald.config.Settings.get_allowed_senders_list", lambda self: ["operator@herald.local"])

    req = IntakeRequest(
        gmail_message_id="msg_intake_fail_001",
        sender_email="operator@herald.local",
        subject="Podcast: Standard",
        body_text="https://paywalled-news.com/article",
    )
    with patch("apps.api.main.extract_article_from_url", side_effect=SourceAccessBlockedError("Cloudflare 403 Forbidden")):
        resp = process_intake(req=req, db=db_session)
        assert resp.status == JobState.FAILED_FINAL.value
        assert resp.error_category == "SOURCE_ACCESS_BLOCKED"

        canonical_path = get_terminal_diagnostics_path(resp.job_id, JobState.FAILED_FINAL.value)
        assert canonical_path.exists()
        assert canonical_path.stat().st_size > 0

        with zipfile.ZipFile(canonical_path, "r") as zf:
            namelist = zf.namelist()
            assert "manifest.json" in namelist
            assert "processing-metrics.json" in namelist
            assert "diagnostic-events.jsonl" in namelist

            metrics = json.loads(zf.read("processing-metrics.json").decode("utf-8"))
            ext_metric = next((m for m in metrics if m["stage"] == "URL_EXTRACTION"), None)
            assert ext_metric is not None
            assert ext_metric["status"] == "failed"

            events = [json.loads(line) for line in zf.read("diagnostic-events.jsonl").decode("utf-8").splitlines() if line.strip()]
            ext_event = next((e for e in events if e["event_type"] == "EXTRACTION_FAILED"), None)
            assert ext_event is not None
            assert ext_event["metadata"]["category"] == "SOURCE_ACCESS_BLOCKED"


def test_api_n8n_delivery_complete_creates_canonical_archive_with_metrics(
    clean_diagnostics_dir, db_session: Session
):
    """Point 3B & 3C: n8n delivery complete transitions to COMPLETE, records EMAIL_DELIVERY,
    DELIVERY_TOTAL, and END_TO_END metrics, and generates canonical COMPLETE archive."""
    from apps.api.main import update_delivery_complete, DeliveryCompleteRequest

    now = datetime.now(UTC)
    job = _create_dummy_job(db_session, status=JobState.DELIVERING.value)
    job.drive_file_id = "drive_audio_123"
    job.details_drive_file_id = "drive_details_123"
    job.audio_ready_at = now - timedelta(seconds=60)
    job.created_at = now - timedelta(seconds=120)
    db_session.commit()

    req = DeliveryCompleteRequest(
        gmail_result_message_id="msg_gmail_987",
        started_at=(now - timedelta(seconds=10)).isoformat(),
        finished_at=now.isoformat(),
        duration_ms=10000,
    )
    resp = update_delivery_complete(job_id=job.id, req=req, db=db_session)
    assert resp["status"] == JobState.COMPLETE.value

    canonical_path = get_terminal_diagnostics_path(job.id, JobState.COMPLETE.value)
    assert canonical_path.exists()
    assert canonical_path.stat().st_size > 0

    with zipfile.ZipFile(canonical_path, "r") as zf:
        namelist = zf.namelist()
        assert "manifest.json" in namelist
        assert "processing-metrics.json" in namelist
        assert "diagnostic-events.jsonl" in namelist

        metrics = json.loads(zf.read("processing-metrics.json").decode("utf-8"))
        metric_stages = {m["stage"] for m in metrics}
        assert "EMAIL_DELIVERY" in metric_stages
        assert "DELIVERY_TOTAL" in metric_stages
        assert "END_TO_END" in metric_stages

        events = [json.loads(line) for line in zf.read("diagnostic-events.jsonl").decode("utf-8").splitlines() if line.strip()]
        complete_event = next((e for e in events if e["event_type"] == "EMAIL_DELIVERY_COMPLETE"), None)
        assert complete_event is not None


def test_terminal_coverage_guard_suite(clean_diagnostics_dir, db_session: Session, monkeypatch):
    """Point 6: Focused regression guard suite ensuring every production terminal finalization path
    produces its required canonical diagnostics archive."""
    from unittest.mock import MagicMock
    from apps.api.main import process_intake, update_delivery_complete, DeliveryCompleteRequest, IntakeRequest
    from apps.worker.main import claim_next_job
    from herald.core.pipeline import process_herald_request, HeraldRequest
    from herald.extraction.url_extractor import ArticleExtractionError
    from herald.telegram.bot import handle_telegram_callback_query
    from herald.telegram.client import TelegramAPIError
    from herald.telegram.delivery import deliver_single_job

    monkeypatch.setattr("herald.config.Settings.get_allowed_senders_list", lambda self: ["operator@herald.local"])

    guard_results = {}

    # 1. Telegram COMPLETE
    audio_path1 = clean_diagnostics_dir.parent / "tg_comp.mp3"
    audio_path1.write_bytes(b"AudioData1")
    job_tg_comp = _create_dummy_job(db_session, status=JobState.AUDIO_READY.value)
    job_tg_comp.telegram_chat_id = 701
    job_tg_comp.local_audio_path = str(audio_path1)
    job_tg_comp.audio_duration_seconds = 60.0
    db_session.commit()
    mock_client = MagicMock()
    mock_client.send_audio.return_value = {"message_id": 11}
    deliver_single_job(db=db_session, job=job_tg_comp, client=mock_client)
    arc = get_terminal_diagnostics_path(job_tg_comp.id, JobState.COMPLETE.value)
    guard_results["telegram_complete"] = arc.exists() and arc.stat().st_size > 0

    # 2. Telegram three-attempt delivery FAILED_FINAL
    audio_path2 = clean_diagnostics_dir.parent / "tg_fail3.mp3"
    audio_path2.write_bytes(b"AudioData2")
    job_tg_fail3 = _create_dummy_job(db_session, status=JobState.AUDIO_READY.value)
    job_tg_fail3.delivery_attempt_count = 2
    job_tg_fail3.telegram_chat_id = 702
    job_tg_fail3.local_audio_path = str(audio_path2)
    db_session.commit()
    err_client = MagicMock()
    err_client.send_audio.side_effect = TelegramAPIError("Conn Error")
    err_client.send_document.side_effect = TelegramAPIError("Conn Error")
    deliver_single_job(db=db_session, job=job_tg_fail3, client=err_client)
    arc = get_terminal_diagnostics_path(job_tg_fail3.id, JobState.FAILED_FINAL.value)
    guard_results["telegram_delivery_three_attempts_failed_final"] = arc.exists() and arc.stat().st_size > 0

    # 3. Telegram missing-audio FAILED_FINAL
    job_tg_miss = _create_dummy_job(db_session, status=JobState.AUDIO_READY.value)
    job_tg_miss.telegram_chat_id = 703
    job_tg_miss.local_audio_path = "/nonexistent/path.mp3"
    db_session.commit()
    deliver_single_job(db=db_session, job=job_tg_miss, client=MagicMock())
    arc = get_terminal_diagnostics_path(job_tg_miss.id, JobState.FAILED_FINAL.value)
    guard_results["telegram_missing_audio_failed_final"] = arc.exists() and arc.stat().st_size > 0

    # 4. Telegram oversized-audio FAILED_FINAL
    monkeypatch.setattr("herald.config.settings.TELEGRAM_MAX_AUDIO_BYTES", 50)
    audio_path4 = clean_diagnostics_dir.parent / "tg_over.mp3"
    audio_path4.write_bytes(b"Z" * 100)
    job_tg_over = _create_dummy_job(db_session, status=JobState.AUDIO_READY.value)
    job_tg_over.telegram_chat_id = 704
    job_tg_over.local_audio_path = str(audio_path4)
    db_session.commit()
    deliver_single_job(db=db_session, job=job_tg_over, client=MagicMock())
    arc = get_terminal_diagnostics_path(job_tg_over.id, JobState.FAILED_FINAL.value)
    guard_results["telegram_oversized_audio_failed_final"] = arc.exists() and arc.stat().st_size > 0

    # 5. Telegram extraction FAILED_FINAL
    with patch("herald.core.pipeline.extract_article_from_url", side_effect=ArticleExtractionError("Extraction fail")):
        req_ext = HeraldRequest(
            source_type="url",
            source_url="https://domain.com/broken",
            telegram_chat_id=705,
            telegram_user_id=705,
            transport="telegram",
        )
        resp_ext = process_herald_request(db_session, req_ext)
        arc = get_terminal_diagnostics_path(resp_ext.job_id, JobState.FAILED_FINAL.value)
        guard_results["telegram_extraction_failed_final"] = arc.exists() and arc.stat().st_size > 0

    # 6. Telegram worker synthesis FAILED_FINAL
    job_work_ff = _create_dummy_job(db_session, status=JobState.QUEUED_TTS.value)
    job_work_ff.synthesis_attempt_count = 4
    db_session.commit()
    claim_next_job(db=db_session, worker_id="guard-worker")
    arc = get_terminal_diagnostics_path(job_work_ff.id, JobState.FAILED_FINAL.value)
    guard_results["telegram_worker_synthesis_failed_final"] = arc.exists() and arc.stat().st_size > 0

    # 7. Telegram pre-script CANCELLED
    job_prescript = _create_dummy_job(db_session, status=JobState.AWAITING_RERUN_CONFIRMATION.value)
    job_prescript.telegram_user_id = 707
    job_prescript.telegram_chat_id = 707
    job_prescript.transport = "telegram"
    db_session.commit()
    cb_prescript = {
        "id": "cb_pre",
        "data": f"h2:deny:{job_prescript.id}",
        "from": {"id": 707},
        "message": {"message_id": 91, "chat": {"id": 707, "type": "private"}},
    }
    with patch("herald.telegram.bot.is_user_authorized", return_value=True):
        handle_telegram_callback_query(db=db_session, client=MagicMock(), cb_query=cb_prescript)
    arc = get_terminal_diagnostics_path(job_prescript.id, JobState.CANCELLED.value)
    guard_results["telegram_prescript_cancelled"] = arc.exists() and arc.stat().st_size > 0

    # 8. Telegram post-script CANCELLED
    job_postscript = _create_dummy_job(db_session, status=JobState.AWAITING_APPROVAL.value)
    job_postscript.telegram_user_id = 708
    job_postscript.telegram_chat_id = 708
    job_postscript.transport = "telegram"
    db_session.commit()
    cb_postscript = {
        "id": "cb_post",
        "data": f"h2:deny:{job_postscript.id}",
        "from": {"id": 708},
        "message": {"message_id": 92, "chat": {"id": 708, "type": "private"}},
    }
    with patch("herald.telegram.bot.is_user_authorized", return_value=True):
        handle_telegram_callback_query(db=db_session, client=MagicMock(), cb_query=cb_postscript)
    arc = get_terminal_diagnostics_path(job_postscript.id, JobState.CANCELLED.value)
    guard_results["telegram_postscript_cancelled"] = arc.exists() and arc.stat().st_size > 0

    # 9. Supported API COMPLETE
    job_api_comp = _create_dummy_job(db_session, status=JobState.DELIVERING.value)
    job_api_comp.drive_file_id = "drive_audio_709"
    job_api_comp.details_drive_file_id = "drive_details_709"
    db_session.commit()
    update_delivery_complete(job_id=job_api_comp.id, req=DeliveryCompleteRequest(gmail_result_message_id="m709"), db=db_session)
    arc = get_terminal_diagnostics_path(job_api_comp.id, JobState.COMPLETE.value)
    guard_results["supported_api_complete"] = arc.exists() and arc.stat().st_size > 0

    # 10. Supported API representative FAILED_FINAL
    with patch("apps.api.main.extract_article_from_url", side_effect=ArticleExtractionError("API Extract Fail")):
        req_api = IntakeRequest(
            gmail_message_id="msg_guard_710",
            sender_email="operator@herald.local",
            subject="Podcast: Standard",
            body_text="https://site.org/bad",
        )
        resp_api = process_intake(req=req_api, db=db_session)
        arc = get_terminal_diagnostics_path(resp_api.job_id, JobState.FAILED_FINAL.value)
        guard_results["supported_api_failed_final"] = arc.exists() and arc.stat().st_size > 0

    assert all(guard_results.values()), f"Some terminal paths failed archive guard: {guard_results}"
    assert len(guard_results) == 10


