from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.main import app
from herald.config import settings
from herald.db.models import JobState


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
    # 1. Perform intake with verify=True
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

        # No acknowledgment email in script generation response
        assert "acknowledgment_email_text" not in gen_data
        assert "acknowledgment_email_html" not in gen_data

        # Execution order verified: script generation first, then verify audit
        assert execution_order == ["SCRIPT_GENERATION", "VERIFY_AUDIT"]
        assert gen_data["status"] == JobState.QUEUED_TTS.value
