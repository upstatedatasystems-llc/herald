from fastapi.testclient import TestClient

from apps.api.main import app
from herald.config import settings
from herald.db.models import JobState, PodcastJob

client = TestClient(app)


def test_failure_matrix_unauthorized_sender(monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "production")
    monkeypatch.setattr(settings, "HERALD_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EMAIL_ALLOWED_SENDERS", "allowed@example.com")

    res = client.post(
        "/api/v1/intake",
        json={
            "gmail_message_id": "msg-matrix-1",
            "sender_email": "unauthorized@example.com",
            "subject": "Podcast: Standard",
            "body_text": "Sample text body",
        },
        headers={"X-API-Key": "test-key"},
    )
    assert res.status_code == 403


def test_failure_matrix_unsupported_subject(monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")

    res = client.post(
        "/api/v1/intake",
        json={
            "gmail_message_id": "msg-matrix-2",
            "sender_email": "user@example.com",
            "subject": "Weekly Podcast: Briefing",
            "body_text": "Sample text body",
        },
    )
    assert res.status_code == 400


def test_failure_matrix_drive_retry_reuses_existing_file(db_session, monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")

    job = PodcastJob(
        gmail_message_id="msg-matrix-3",
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-matrix-3",
        source_text="Sample text body",
        local_audio_path="/data/herald/output/test.mp3",
        audio_bytes=1000000,
        audio_duration_seconds=30,
        drive_file_id="existing-drive-file-id",
        drive_web_link="https://drive.google.com/file/d/existing-drive-file-id",
        status=JobState.DELIVERING.value,
        script_json={
            "episode_title": "Matrix Test",
            "episode_description": "Desc",
            "estimated_minutes": 1,
            "segments": [{"order": 1, "heading": "Intro", "narration": "Narration"}],
            "warnings": [],
        },
    )
    db_session.add(job)
    db_session.commit()

    res = client.post("/api/v1/delivery/claim")
    assert res.status_code == 200
    data = res.json()
    assert data["claimed"] is True
    assert data["job"]["needs_audio_upload"] is False
    assert data["job"]["drive_file_id"] == "existing-drive-file-id"
