from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from herald.config import settings
from herald.db.models import JobState, PodcastJob, RequestMode, SourceType

client = TestClient(app)
AUTH_HEADERS = {"X-API-Key": "test-api-key"}


def test_resumable_delivery_partial_artifacts(db_session, monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "test")

    now = datetime.now(UTC)
    job = PodcastJob(
        id="resumable-job-001",
        gmail_message_id="res-msg-1",
        sender_email="auth@example.com",
        request_mode=RequestMode.STANDARD.value,
        source_type=SourceType.EMAIL_BODY.value,
        source_hash="reshash1",
        source_text="Test source text content for delivery",
        status=JobState.AUDIO_READY.value,
        local_audio_path=str(Path(settings.HERALD_WORK_DIR) / "output" / "test_audio.mp3"),
        audio_bytes=1000,
        audio_duration_seconds=60,
        audio_sha256="sha123",
        created_at=now,
        audio_ready_at=now,
        script_json={"episode_title": "Resumable Title", "estimated_minutes": 2.0, "segments": [{}]},
    )
    db_session.add(job)
    db_session.commit()

    # 1. First claim: no Drive IDs exist yet
    res = client.post("/api/v1/delivery/claim", headers=AUTH_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["claimed"] is True
    job_data = data["job"]
    assert job_data["needs_audio_upload"] is True
    assert job_data["needs_source_upload"] is True
    assert job_data["needs_diagnostics_upload"] is True
    # Email is NOT formatted at claim time
    assert "formatted_email_text" not in job_data
    assert "formatted_email_html" not in job_data

    # 2. Attempting to fetch completion email before all 3 Drive IDs exist fails with 400
    res_premature_email = client.get(f"/api/v1/jobs/{job.id}/completion-email", headers=AUTH_HEADERS)
    assert res_premature_email.status_code == 400
    assert "missing required Drive artifact IDs" in res_premature_email.json()["detail"]

    # 3. Record audio upload only
    res_audio = client.post(
        f"/api/v1/jobs/{job.id}/drive-complete",
        headers=AUTH_HEADERS,
        json={"artifact_type": "audio", "drive_file_id": "audio-drive-id-1", "drive_web_link": "http://drive/audio"},
    )
    assert res_audio.status_code == 200
    assert res_audio.json()["drive_file_id"] == "audio-drive-id-1"

    # 4. Reset claimed_at to simulate a stale retry claim after audio upload
    job.claimed_at = now - timedelta(minutes=20)
    db_session.commit()

    res2 = client.post("/api/v1/delivery/claim", headers=AUTH_HEADERS)
    assert res2.status_code == 200
    j2 = res2.json()["job"]
    assert j2["needs_audio_upload"] is False
    assert j2["needs_source_upload"] is True
    assert j2["needs_diagnostics_upload"] is True

    # 5. Record source upload
    res_src = client.post(
        f"/api/v1/jobs/{job.id}/drive-complete",
        headers=AUTH_HEADERS,
        json={"artifact_type": "source", "source_drive_file_id": "source-drive-id-2", "source_drive_web_link": "http://drive/source"},
    )
    assert res_src.status_code == 200

    # 6. Attempt delivery-complete before diagnostics is uploaded -> should fail with 400
    res_premature = client.post(
        f"/api/v1/jobs/{job.id}/delivery-complete",
        headers=AUTH_HEADERS,
        json={"gmail_result_message_id": "result-msg-1"},
    )
    assert res_premature.status_code == 400
    assert "missing required Drive artifact IDs" in res_premature.json()["detail"]

    # 7. Record diagnostics upload
    res_diag = client.post(
        f"/api/v1/jobs/{job.id}/drive-complete",
        headers=AUTH_HEADERS,
        json={"artifact_type": "diagnostics", "diagnostics_drive_file_id": "diag-drive-id-3", "diagnostics_drive_web_link": "http://drive/diag"},
    )
    assert res_diag.status_code == 200

    # 7b. Record script upload
    res_script = client.post(
        f"/api/v1/jobs/{job.id}/drive-complete",
        headers=AUTH_HEADERS,
        json={"artifact_type": "script", "script_drive_file_id": "script-drive-id-4", "script_drive_web_link": "http://drive/script"},
    )
    assert res_script.status_code == 200

    # 8. Now fetching completion email succeeds and contains fresh Drive links!
    res_email = client.get(f"/api/v1/jobs/{job.id}/completion-email", headers=AUTH_HEADERS)
    assert res_email.status_code == 200
    email_payload = res_email.json()
    assert "http://drive/audio" in email_payload["html"]
    assert "http://drive/source" in email_payload["html"]
    assert "http://drive/diag" in email_payload["html"]
    assert "Listen on Google Drive" in email_payload["html"]

    # 9. Now delivery-complete succeeds!
    res_final = client.post(
        f"/api/v1/jobs/{job.id}/delivery-complete",
        headers=AUTH_HEADERS,
        json={"gmail_result_message_id": "result-msg-1"},
    )
    assert res_final.status_code == 200
    assert res_final.json()["status"] == JobState.COMPLETE.value


def test_cleanup_all_three_local_artifacts(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "test")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))

    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    mp3_file = output_dir / "test_cleanup.mp3"
    src_file = output_dir / "test_cleanup_source.txt"
    diag_file = output_dir / "test_cleanup_diagnostics.md"

    mp3_file.write_bytes(b"dummy mp3 data")
    src_file.write_text("dummy source text")
    diag_file.write_text("# Diagnostics")

    old_completed = datetime.now(UTC) - timedelta(hours=50)

    job = PodcastJob(
        id="cleanup-job-001",
        gmail_message_id="cleanup-msg-1",
        sender_email="auth@example.com",
        request_mode=RequestMode.STANDARD.value,
        source_type=SourceType.EMAIL_BODY.value,
        source_hash="cleanuphash1",
        source_text="source",
        status=JobState.COMPLETE.value,
        local_audio_path=str(mp3_file),
        completed_at=old_completed,
        created_at=old_completed,
    )
    db_session.add(job)
    db_session.commit()

    res = client.post("/api/v1/ops/cleanup", headers=AUTH_HEADERS)
    assert res.status_code == 200

    assert not mp3_file.exists()
    assert not src_file.exists()
    assert not diag_file.exists()
