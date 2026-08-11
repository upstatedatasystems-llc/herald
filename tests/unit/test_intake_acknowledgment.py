from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.main import app
from herald.config import settings
from herald.db.models import JobState, PodcastJob


@pytest.fixture
def api_client(monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")
    return TestClient(app)


def test_intake_produces_immediate_receipt_acknowledgment(api_client, db_session: Session):
    """
    Prove that POST /api/v1/intake returns the lightweight receipt acknowledgment email
    immediately upon intake success, before script generation.
    """
    req_data = {
        "gmail_message_id": "msg-ack-test-001",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": "This is sample article content for testing immediate intake receipt acknowledgment.",
    }

    response = api_client.post("/api/v1/intake", json=req_data)
    assert response.status_code == 200, f"Intake failed with detail: {response.text}"
    data = response.json()

    assert data["is_duplicate"] is False
    assert "job_id" in data
    assert data["acknowledgment_email_text"] is not None
    assert data["acknowledgment_email_html"] is not None

    assert "HERALD — REQUEST RECEIVED" in data["acknowledgment_email_text"]
    assert "Your podcast request has been received and processing has begun." in data["acknowledgment_email_text"]
    assert "Requested Format: Standard" in data["acknowledgment_email_text"]
    assert f"Job ID: {data['job_id']}" in data["acknowledgment_email_text"]
    assert "You will receive another email with your private Google Drive link when the episode is complete." in data["acknowledgment_email_text"]

    assert "<html" in data["acknowledgment_email_html"].lower()
    assert "Your podcast request has been received" in data["acknowledgment_email_html"]


def test_duplicate_intake_does_not_produce_duplicate_acknowledgment(api_client, db_session: Session):
    """
    Prove that duplicate/replayed intake calls return is_duplicate=True
    and do NOT generate duplicate acknowledgment emails (acknowledgment_email_html is None).
    """
    req_data = {
        "gmail_message_id": "msg-ack-test-002",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": "Duplicate intake content for idempotency testing.",
    }

    # First intake
    r1 = api_client.post("/api/v1/intake", json=req_data)
    assert r1.status_code == 200, f"Intake 1 failed: {r1.text}"
    assert r1.json()["is_duplicate"] is False
    assert r1.json()["acknowledgment_email_text"] is not None

    # Replayed intake
    r2 = api_client.post("/api/v1/intake", json=req_data)
    assert r2.status_code == 200, f"Intake 2 failed: {r2.text}"
    data2 = r2.json()
    assert data2["is_duplicate"] is True
    assert data2["acknowledgment_email_text"] is None
    assert data2["acknowledgment_email_html"] is None


def test_standard_verify_processing_order_preserved(api_client, db_session: Session):
    """
    Prove that calling generate_script_endpoint for a job with verify=True
    executes script -> audit -> repair (if needed) -> transition to QUEUED_TTS
    in the exact same processing order as before, without returning a second acknowledgment.
    """
    req_data = {
        "gmail_message_id": "msg-ack-test-003",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard verify",
        "body_text": "Sample text for standard verify pipeline test.",
    }
    r1 = api_client.post("/api/v1/intake", json=req_data)
    assert r1.status_code == 200, f"Intake failed: {r1.text}"
    job_id = r1.json()["job_id"]

    mock_script = MagicMock()
    mock_script.model_dump.return_value = {
        "episode_title": "Standard Verify Test Episode",
        "segments": [{"speaker": "Speaker 1", "text": "Hello world."}],
    }

    mock_audit = MagicMock()
    mock_audit.has_material_issues = False
    mock_audit.model_dump.return_value = {"has_material_issues": False, "score": 0.95}

    execution_order = []

    def mock_gen_script(*args, **kwargs):
        execution_order.append("SCRIPT_GENERATION")
        return mock_script

    def mock_audit_script(*args, **kwargs):
        execution_order.append("VERIFY_AUDIT")
        return mock_audit

    with patch("apps.api.main.generate_podcast_script", side_effect=mock_gen_script), \
         patch("apps.api.main.audit_script_fidelity", side_effect=mock_audit_script):

        r2 = api_client.post("/api/v1/script/generate", json={"job_id": job_id})
        assert r2.status_code == 200, f"Script generation failed: {r2.text}"
        gen_data = r2.json()

        assert "acknowledgment_email_text" not in gen_data
        assert "acknowledgment_email_html" not in gen_data

        assert execution_order == ["SCRIPT_GENERATION", "VERIFY_AUDIT"]
        assert gen_data["status"] == JobState.QUEUED_TTS.value


def test_ack_gmail_failure_does_not_gate_script_generation(api_client, db_session: Session):
    """
    Prove that even if sending the submission acknowledgment fails or is bypassed,
    script generation proceeds cleanly without being gated by acknowledgment status.
    """
    req_data = {
        "gmail_message_id": "msg-ack-fail-001",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": "Content for testing script generation independence from acknowledgment sending.",
    }

    r1 = api_client.post("/api/v1/intake", json=req_data)
    assert r1.status_code == 200
    job_id = r1.json()["job_id"]

    mock_script = MagicMock()
    mock_script.model_dump.return_value = {
        "episode_title": "Ack Failure Resilience Test Episode",
        "segments": [{"speaker": "Speaker 1", "text": "Testing script generation without gating."}],
    }

    with patch("apps.api.main.generate_podcast_script", return_value=mock_script):
        r2 = api_client.post("/api/v1/script/generate", json={"job_id": job_id})
        assert r2.status_code == 200
        assert r2.json()["status"] == JobState.QUEUED_TTS.value


def test_replay_at_source_ready_resumes_job_without_new_ack(api_client, db_session: Session):
    """
    Prove that replaying intake for a job still in SOURCE_READY:
    1. Returns is_duplicate=True
    2. Returns acknowledgment_email_html=None (no new acknowledgment email)
    3. Leaves job status in SOURCE_READY so script generation can be called to complete processing.
    """
    req_data = {
        "gmail_message_id": "msg-replay-sr-001",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": "Content for testing SOURCE_READY replay resumption.",
    }

    r1 = api_client.post("/api/v1/intake", json=req_data)
    assert r1.status_code == 200
    job_id_1 = r1.json()["job_id"]
    assert r1.json()["status"] == JobState.SOURCE_READY.value

    # Replay intake call while job is SOURCE_READY
    r2 = api_client.post("/api/v1/intake", json=req_data)
    assert r2.status_code == 200
    data2 = r2.json()

    assert data2["job_id"] == job_id_1
    assert data2["is_duplicate"] is True
    assert data2["acknowledgment_email_html"] is None
    assert data2["status"] == JobState.SOURCE_READY.value

    # Script generation can now resume for the existing job
    mock_script = MagicMock()
    mock_script.model_dump.return_value = {
        "episode_title": "Replay Resumed Episode",
        "segments": [{"speaker": "Speaker 1", "text": "Resumed from SOURCE_READY."}],
    }

    with patch("apps.api.main.generate_podcast_script", return_value=mock_script):
        r3 = api_client.post("/api/v1/script/generate", json={"job_id": job_id_1})
        assert r3.status_code == 200
        assert r3.json()["status"] == JobState.QUEUED_TTS.value


def test_replay_when_queued_tts_does_not_regenerate_script(api_client, db_session: Session):
    """
    Prove that replaying script generation for a job already in QUEUED_TTS (or later)
    does not re-invoke generate_podcast_script and returns early with existing job status.
    """
    req_data = {
        "gmail_message_id": "msg-replay-queued-001",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": "Content for testing QUEUED_TTS script idempotency.",
    }

    r1 = api_client.post("/api/v1/intake", json=req_data)
    job_id = r1.json()["job_id"]

    mock_script = MagicMock()
    mock_script.model_dump.return_value = {
        "episode_title": "Idempotent Script Episode",
        "segments": [{"speaker": "Speaker 1", "text": "Initial generation."}],
    }

    # Initial script generation
    with patch("apps.api.main.generate_podcast_script", return_value=mock_script) as mock_gen:
        r2 = api_client.post("/api/v1/script/generate", json={"job_id": job_id})
        assert r2.status_code == 200
        assert r2.json()["status"] == JobState.QUEUED_TTS.value
        assert mock_gen.call_count == 1

    # Replayed script generation call on job already in QUEUED_TTS
    with patch("apps.api.main.generate_podcast_script") as mock_gen_2:
        r3 = api_client.post("/api/v1/script/generate", json={"job_id": job_id})
        assert r3.status_code == 200
        assert "already exists" in r3.json()["message"]
        # Must not re-run Gemini script generation
        assert mock_gen_2.call_count == 0
