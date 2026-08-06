from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from apps.api.main import app
from herald.config import settings
from herald.db.models import JobState, PodcastJob

client = TestClient(app)


def test_ops_endpoints(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))

    # Create COMPLETE job > 48h with local audio path
    old_time = datetime.now(UTC) - timedelta(hours=50)
    audio_file = tmp_path / "old_audio.mp3"
    audio_file.write_bytes(b"dummy mp3 data")

    job_old = PodcastJob(
        gmail_message_id="msg-ops-old",
        sender_email="test@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-ops-old",
        source_text="Ops text old",
        status=JobState.COMPLETE.value,
        completed_at=old_time,
        local_audio_path=str(audio_file),
    )
    db_session.add(job_old)
    db_session.commit()

    # 1. Test Cleanup API
    res_cleanup = client.post("/api/v1/ops/cleanup")
    assert res_cleanup.status_code == 200
    data_clean = res_cleanup.json()
    assert data_clean["cleaned_jobs_count"] == 1
    assert not audio_file.exists()

    # 2. Test Daily Health API
    res_health = client.get("/api/v1/ops/daily-health")
    assert res_health.status_code == 200
    data_health = res_health.json()
    assert "queue_counts" in data_health
    assert "readiness_status" in data_health

    # 3. Test Weekly Maintenance API
    res_maint = client.post("/api/v1/ops/weekly-maintenance")
    assert res_maint.status_code == 200
    assert res_maint.json()["status"] == "completed"

    # 4. Test Error Handler API
    res_err = client.post(
        "/api/v1/ops/error-handler",
        json={
            "job_id": job_old.id,
            "error_code": "TEST_ERROR",
            "error_detail": "Test error detail",
            "failed_stage": "DELIVERING",
        },
    )
    assert res_err.status_code == 200
    assert res_err.json()["alert"] == "HERALD_SYSTEM_ERROR"
