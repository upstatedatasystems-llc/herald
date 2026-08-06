from fastapi.testclient import TestClient

from apps.api.main import app
from herald.config import settings
from herald.db.models import JobState, PodcastJob

client = TestClient(app)


def test_delivery_claim_endpoint_and_idempotency(db_session, monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")

    job_id = "job-delivery-test-100"
    job = PodcastJob(
        id=job_id,
        gmail_message_id="msg-delivery-1",
        sender_email="test@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-delivery-1",
        source_text="Delivery test text",
        local_audio_path="/data/herald/output/test.mp3",
        audio_bytes=5000000,
        audio_duration_seconds=120,
        status=JobState.AUDIO_READY.value,
        script_json={
            "episode_title": "Delivery Title",
            "episode_description": "Delivery Desc",
            "estimated_minutes": 2,
            "segments": [{"order": 1, "heading": "Intro", "narration": "Narration text"}],
            "warnings": [],
        },
    )
    db_session.add(job)
    db_session.commit()

    res = client.post("/api/v1/delivery/claim")
    assert res.status_code == 200
    data = res.json()
    assert data["claimed"] is True
    assert data["job"]["id"] == job_id
    assert data["job"]["needs_upload"] is True
    assert data["job"]["needs_email"] is True
    assert isinstance(data["job"]["script_json"], dict)

    res_drive = client.post(
        f"/api/v1/jobs/{job_id}/drive-complete",
        json={"drive_file_id": "file-123", "drive_web_link": "https://drive.google.com/file/d/file-123"},
    )
    assert res_drive.status_code == 200
    assert res_drive.json()["status"] == JobState.DELIVERING.value

    # Repeating drive-complete with same ID -> 200 OK
    res_repeat = client.post(
        f"/api/v1/jobs/{job_id}/drive-complete",
        json={"drive_file_id": "file-123", "drive_web_link": "https://drive.google.com/file/d/file-123"},
    )
    assert res_repeat.status_code == 200

    # Repeating drive-complete with conflicting ID -> 409 Conflict
    res_conflict = client.post(
        f"/api/v1/jobs/{job_id}/drive-complete",
        json={"drive_file_id": "file-conflicting-999", "drive_web_link": "https://drive.google.com/file/d/file-999"},
    )
    assert res_conflict.status_code == 409

    # Complete delivery
    res_del = client.post(
        f"/api/v1/jobs/{job_id}/delivery-complete",
        json={"gmail_result_message_id": "gmail-res-123"},
    )
    assert res_del.status_code == 200
    assert res_del.json()["status"] == JobState.COMPLETE.value

    # Modifying COMPLETE job metadata -> 409 Conflict
    res_complete_mod = client.post(
        f"/api/v1/jobs/{job_id}/drive-complete",
        json={"drive_file_id": "file-123", "drive_web_link": "https://drive.google.com/file/d/file-123"},
    )
    assert res_complete_mod.status_code == 409
