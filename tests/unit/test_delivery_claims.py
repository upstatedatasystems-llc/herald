from datetime import UTC, datetime

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
    assert data["action"] == "deliver_artifacts_and_email"
    assert data["job"]["id"] == job_id
    assert data["job"]["needs_audio_upload"] is True
    assert data["job"]["needs_source_upload"] is True
    assert data["job"]["needs_diagnostics_upload"] is True
    assert data["job"]["needs_email"] is True
    assert isinstance(data["job"]["script_json"], dict)

    res_drive = client.post(
        f"/api/v1/jobs/{job_id}/drive-complete",
        json={"artifact_type": "audio", "drive_file_id": "file-123", "drive_web_link": "https://drive.google.com/file/d/file-123"},
    )
    assert res_drive.status_code == 200
    assert res_drive.json()["status"] == JobState.DELIVERING.value

    # Record remaining source, script, and diagnostics Drive IDs
    client.post(
        f"/api/v1/jobs/{job_id}/drive-complete",
        json={"artifact_type": "source", "source_drive_file_id": "source-123", "source_drive_web_link": "https://drive.google.com/source-123"},
    )
    client.post(
        f"/api/v1/jobs/{job_id}/drive-complete",
        json={"artifact_type": "script", "script_drive_file_id": "script-123", "script_drive_web_link": "https://drive.google.com/script-123"},
    )
    client.post(
        f"/api/v1/jobs/{job_id}/drive-complete",
        json={"artifact_type": "diagnostics", "diagnostics_drive_file_id": "diag-123", "diagnostics_drive_web_link": "https://drive.google.com/diag-123"},
    )

    # Repeating drive-complete with same ID -> 200 OK
    res_repeat = client.post(
        f"/api/v1/jobs/{job_id}/drive-complete",
        json={"artifact_type": "audio", "drive_file_id": "file-123", "drive_web_link": "https://drive.google.com/file/d/file-123"},
    )
    assert res_repeat.status_code == 200

    # Repeating drive-complete with conflicting ID -> 409 Conflict
    res_conflict = client.post(
        f"/api/v1/jobs/{job_id}/drive-complete",
        json={"artifact_type": "audio", "drive_file_id": "file-conflicting-999", "drive_web_link": "https://drive.google.com/file/d/file-999"},
    )
    assert res_conflict.status_code == 409

    # Complete delivery
    res_del = client.post(
        f"/api/v1/jobs/{job_id}/delivery-complete",
        json={"gmail_result_message_id": "gmail-res-123"},
    )
    assert res_del.status_code == 200
    assert res_del.json()["status"] == JobState.COMPLETE.value

    # Modifying COMPLETE job metadata with conflicting Drive ID -> 409 Conflict
    res_complete_mod = client.post(
        f"/api/v1/jobs/{job_id}/drive-complete",
        json={"drive_file_id": "file-conflicting-888", "drive_web_link": "https://drive.google.com/file/d/file-888"},
    )
    assert res_complete_mod.status_code == 409


def test_no_duplicate_gmail_delivery_and_action(db_session, monkeypatch):
    """Test claiming job with existing delivered_at returns complete_without_resend and needs_email=False."""
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")

    job_id = "job-delivered-prevent-dup"
    job = PodcastJob(
        id=job_id,
        gmail_message_id="msg-delivered-1",
        sender_email="test@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-delivered-1",
        source_text="Delivered text",
        local_audio_path="/data/herald/output/test_delivered.mp3",
        drive_file_id="drive-123",
        drive_web_link="https://drive.google.com/file/d/drive-123",
        source_drive_file_id="source-123",
        source_drive_web_link="https://drive.google.com/source-123",
        script_drive_file_id="script-123",
        script_drive_web_link="https://drive.google.com/script-123",
        diagnostics_drive_file_id="diag-123",
        diagnostics_drive_web_link="https://drive.google.com/diag-123",
        gmail_result_message_id="gmail-sent-777",
        delivered_at=datetime.now(UTC),
        status=JobState.DELIVERING.value,
    )
    db_session.add(job)
    db_session.commit()

    res = client.post("/api/v1/delivery/claim")
    assert res.status_code == 200
    data = res.json()
    assert data["claimed"] is True
    assert data["action"] == "complete_without_resend"
    assert data["job"]["needs_email"] is False


def test_research_mode_all_six_artifacts_delivery_flow(db_session, monkeypatch):
    """Test claiming and completing delivery for Research mode requires all 6 artifact Drive IDs."""
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")

    job_id = "job-res-six-artifacts-001"
    job = PodcastJob(
        id=job_id,
        gmail_message_id="msg-res-six-1",
        sender_email="res@example.com",
        request_mode="research",
        research_depth="high",
        source_type="email_body",
        source_hash="hash-res-six-1",
        source_text="Research text",
        status=JobState.AUDIO_READY.value,
        script_json={"episode_title": "Title", "segments": [{"order": 1, "heading": "H", "narration": "N"}], "warnings": []},
        research_json={"source_summary": "Summary", "verification": [], "useful_context": [], "outdated_or_uncertain": [], "research_sources": []},
    )
    db_session.add(job)
    db_session.commit()

    res_claim = client.post("/api/v1/delivery/claim")
    assert res_claim.status_code == 200
    data = res_claim.json()
    assert data["job"]["needs_audio_upload"] is True
    assert data["job"]["needs_source_upload"] is True
    assert data["job"]["needs_script_upload"] is True
    assert data["job"]["needs_diagnostics_upload"] is True
    assert data["job"]["needs_research_upload"] is True
    assert data["job"]["needs_research_notes_upload"] is True

    # Attempting completion with missing research artifacts must fail (400)
    client.post(f"/api/v1/jobs/{job_id}/drive-complete", json={"artifact_type": "audio", "drive_file_id": "a-1", "drive_web_link": "https://a-1"})
    client.post(f"/api/v1/jobs/{job_id}/drive-complete", json={"artifact_type": "source", "source_drive_file_id": "s-1", "source_drive_web_link": "https://s-1"})
    client.post(f"/api/v1/jobs/{job_id}/drive-complete", json={"artifact_type": "script", "script_drive_file_id": "sc-1", "script_drive_web_link": "https://sc-1"})
    client.post(f"/api/v1/jobs/{job_id}/drive-complete", json={"artifact_type": "diagnostics", "diagnostics_drive_file_id": "d-1", "diagnostics_drive_web_link": "https://d-1"})

    res_incomplete = client.post(f"/api/v1/jobs/{job_id}/delivery-complete", json={"gmail_result_message_id": "g-1"})
    assert res_incomplete.status_code == 400
    assert "research" in res_incomplete.json()["detail"]

    # Upload remaining research artifacts
    client.post(f"/api/v1/jobs/{job_id}/drive-complete", json={"artifact_type": "research", "research_drive_file_id": "r-1", "research_drive_web_link": "https://r-1"})
    client.post(f"/api/v1/jobs/{job_id}/drive-complete", json={"artifact_type": "research_notes", "research_notes_drive_file_id": "rn-1", "research_notes_drive_web_link": "https://rn-1"})

    # Now completion succeeds (200)
    res_complete = client.post(f"/api/v1/jobs/{job_id}/delivery-complete", json={"gmail_result_message_id": "g-1"})
    assert res_complete.status_code == 200
    assert res_complete.json()["status"] == JobState.COMPLETE.value

