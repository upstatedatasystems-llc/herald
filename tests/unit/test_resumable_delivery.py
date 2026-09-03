from datetime import UTC, datetime, timedelta
from pathlib import Path

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
        script_json={
            "episode_title": "Resumable Title",
            "estimated_minutes": 2.0,
            "segments": [{}],
        },
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
    assert job_data["needs_details_upload"] is True

    # 2. Attempting to fetch completion email before all Drive IDs exist fails with 400
    res_premature_email = client.get(
        f"/api/v1/jobs/{job.id}/completion-email", headers=AUTH_HEADERS
    )
    assert res_premature_email.status_code == 400
    assert "missing required Drive artifact IDs" in res_premature_email.json()["detail"]

    # 3. Record audio upload only
    res_audio = client.post(
        f"/api/v1/jobs/{job.id}/drive-complete",
        headers=AUTH_HEADERS,
        json={
            "artifact_type": "audio",
            "drive_file_id": "audio-drive-id-1",
            "drive_web_link": "http://drive/audio",
        },
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
    assert j2["needs_details_upload"] is True

    # 5. Attempt delivery-complete before details is uploaded -> should fail with 400
    res_premature = client.post(
        f"/api/v1/jobs/{job.id}/delivery-complete",
        headers=AUTH_HEADERS,
        json={"gmail_result_message_id": "result-msg-1"},
    )
    assert res_premature.status_code == 400
    assert "missing required Drive artifact IDs" in res_premature.json()["detail"]

    # 6. Record details upload
    res_details = client.post(
        f"/api/v1/jobs/{job.id}/drive-complete",
        headers=AUTH_HEADERS,
        json={
            "artifact_type": "details",
            "details_drive_file_id": "details-drive-id-2",
            "details_drive_web_link": "http://drive/details",
        },
    )
    assert res_details.status_code == 200

    # 7. Now fetching completion email succeeds!
    res_email = client.get(f"/api/v1/jobs/{job.id}/completion-email", headers=AUTH_HEADERS)
    assert res_email.status_code == 200
    email_payload = res_email.json()
    assert "http://drive/audio" in email_payload["html"]
    assert "http://drive/details" in email_payload["html"]

    # 8. Now delivery-complete succeeds!
    res_final = client.post(
        f"/api/v1/jobs/{job.id}/delivery-complete",
        headers=AUTH_HEADERS,
        json={"gmail_result_message_id": "result-msg-1"},
    )
    assert res_final.status_code == 200
    assert res_final.json()["status"] == JobState.COMPLETE.value


def test_cleanup_local_artifacts(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "test")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))

    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    mp3_file = output_dir / "test_cleanup.mp3"
    details_file = output_dir / "test_cleanup_details.md"

    mp3_file.write_bytes(b"dummy mp3 data")
    details_file.write_text("# Details")

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
    assert not details_file.exists()
