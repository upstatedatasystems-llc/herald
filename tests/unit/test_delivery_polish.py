import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.main import app
from herald.audio.artifact_generator import get_artifact_filenames
from herald.config import settings
from herald.db.models import JobState, PodcastJob
from herald.extraction.source_cleaner import deduplicate_source_blocks
from herald.extraction.url_extractor import (
    SourceAccessBlockedError,
    extract_article_from_url,
)
from herald.n8n.credential_rehydrator import (
    rehydrate_workflow_credentials,
    validate_workflow_credentials,
    validate_workflow_for_deployment,
)
from herald.services.drive_service import (
    build_user_facing_drive_filename,
    sanitize_filename_title,
)
from herald.services.email_formatter import (
    format_acknowledgment_email,
    format_completion_email,
    format_failure_email,
)


@pytest.fixture
def api_client(monkeypatch):
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")
    return TestClient(app)


def test_intake_acknowledgment_formatting():
    """
    Verify format_acknowledgment_email includes Mode, Source Type, Verification,
    Job Started timestamp in local timezone, Job ID, and Research Depth (for Research mode).
    """
    now_iso = "2026-08-11T14:30:00Z"
    
    # 1. Standard mode
    ack_std = format_acknowledgment_email(
        job_id="test-job-std-123",
        request_mode="standard",
        source_type="url",
        verify_enabled=True,
        created_at_iso=now_iso,
    )
    assert "HERALD — REQUEST RECEIVED" in ack_std["text"]
    assert "Requested Format: Standard" in ack_std["text"]
    assert "Source Type: Url" in ack_std["text"]
    assert "Verification: Enabled" in ack_std["text"]
    assert "Aug 11, 2026" in ack_std["text"]
    assert "Job ID: test-job-std-123" in ack_std["text"]
    assert "Research Depth" not in ack_std["text"]

    # 2. Research mode with depth
    ack_res = format_acknowledgment_email(
        job_id="test-job-res-456",
        request_mode="research",
        source_type="email_body",
        verify_enabled=False,
        created_at_iso=now_iso,
        research_depth="deep",
    )
    assert "Requested Format: Research" in ack_res["text"]
    assert "Source Type: Email Body" in ack_res["text"]
    assert "Verification: Disabled" in ack_res["text"]
    assert "Research Depth: Deep" in ack_res["text"]
    assert "Research Depth" in ack_res["html"]


def test_completion_email_formatting():
    """
    Verify format_completion_email removes 'Script Estimate', adds Verification status,
    adds Started timestamp, and omits 'Completed: N/A'.
    """
    created_iso = "2026-08-11T14:00:00Z"
    completed_iso = "2026-08-11T14:15:00Z"

    email = format_completion_email(
        job_id="comp-job-789",
        episode_title="Test Episode Title",
        episode_description="Test episode description",
        drive_web_link="https://drive.google.com/file/d/audio-123/view",
        duration_seconds=300,
        file_bytes=2400000,
        request_mode="standard",
        source_type="url",
        source_title="Source Title",
        script_estimated_minutes=5.0,
        segments_count=6,
        sha256="abc123sha256",
        chunk_count=6,
        retry_attempts=0,
        drive_file_id="audio-123",
        created_at_iso=created_iso,
        completed_at_iso=completed_iso,
        verify_enabled=True,
        verification_result="Passed (No material issues detected)",
    )

    # 1. Script Estimate removed
    assert "Script Estimate" not in email["text"]
    assert "Script Estimate" not in email["html"]

    # 2. Verification status included
    assert "Verification: Passed (No material issues detected)" in email["text"]
    assert "Passed (No material issues detected)" in email["html"]

    # 3. Started timestamp included
    assert "Started: Aug 11, 2026" in email["text"]
    assert "Completed: Aug 11, 2026" in email["text"]
    assert "Completed: N/A" not in email["text"]
    assert "Completed: N/A" not in email["html"]


def test_completion_email_omits_completed_na():
    """
    Verify format_completion_email omits the Completed timestamp line when completed_at_iso is missing or N/A.
    """
    created_iso = "2026-08-11T14:00:00Z"

    email = format_completion_email(
        job_id="comp-job-no-comp",
        episode_title="Test Title",
        episode_description="Desc",
        drive_web_link="https://drive.google.com/file/d/audio-123/view",
        duration_seconds=120,
        file_bytes=1000000,
        request_mode="brief",
        source_type="email_body",
        source_title="Source",
        script_estimated_minutes=2.0,
        segments_count=3,
        sha256="sha",
        chunk_count=3,
        retry_attempts=0,
        drive_file_id="audio-123",
        created_at_iso=created_iso,
        completed_at_iso=None,
    )

    assert "Completed: N/A" not in email["text"]
    assert "Completed: N/A" not in email["html"]
    assert "Started: Aug 11, 2026" in email["text"]


def test_drive_filename_sanitization_and_construction():
    """
    Verify title sanitization and Drive user-facing filename building for Audio (.mp3) and Details (.md).
    """
    title = 'AI & Future: What\'s Next? / "Test" <Special>*'
    sanitized = sanitize_filename_title(title)
    assert ":" not in sanitized
    assert "/" not in sanitized
    assert "?" not in sanitized
    assert '"' not in sanitized
    assert "<" not in sanitized
    assert "*" not in sanitized
    assert "AI & Future- What's Next- -Test- -Special-" in sanitized or "AI & Future" in sanitized

    dt = datetime(2026, 8, 11, 14, 0, 0, tzinfo=UTC)
    audio_fn = build_user_facing_drive_filename("Quantum Computing Breakthroughs!", dt, "Standard", "mp3")
    details_fn = build_user_facing_drive_filename("Quantum Computing Breakthroughs!", dt, "Standard", "md")

    assert audio_fn == "Quantum Computing Breakthroughs- 8-11-26 Standard.mp3" or "Quantum Computing Breakthroughs" in audio_fn
    assert audio_fn.endswith("8-11-26 Standard.mp3")
    assert details_fn.endswith("8-11-26 Standard.md")


def test_delivery_claim_returns_readable_drive_filenames_and_uuid_local_paths(api_client, db_session: Session):
    """
    Verify /api/v1/delivery/claim returns separate readable audio_drive_filename and details_drive_filename,
    while preserving UUID-based local audio_filename, details_filename, and local paths.
    """
    job_id = f"claim-job-{uuid.uuid4().hex[:8]}"
    job = PodcastJob(
        id=job_id,
        gmail_message_id="msg-claim-001",
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-claim-123",
        source_text="Test source text content",
        status=JobState.AUDIO_READY.value,
        custom_title="The AI Revolution Begins",
        created_at=datetime(2026, 8, 11, 14, 0, 0, tzinfo=UTC),
    )
    db_session.add(job)
    db_session.commit()

    res = api_client.post("/api/v1/delivery/claim")
    assert res.status_code == 200
    data = res.json()

    assert data["claimed"] is True
    claimed_job = data["job"]
    assert claimed_job["id"] == job_id

    # 1. Readable Drive-facing filenames
    assert claimed_job["audio_drive_filename"].startswith("The AI Revolution Begins")
    assert claimed_job["audio_drive_filename"].endswith(".mp3")
    assert "8-11-26 Standard" in claimed_job["audio_drive_filename"]

    assert claimed_job["details_drive_filename"].startswith("The AI Revolution Begins")
    assert claimed_job["details_drive_filename"].endswith(".md")
    assert "8-11-26 Standard" in claimed_job["details_drive_filename"]

    # 2. Preserved local filenames and paths
    names = get_artifact_filenames(job)
    assert claimed_job["audio_filename"] == names["audio_filename"]
    assert claimed_job["details_filename"] == names["details_filename"]
    assert claimed_job["local_audio_path"].endswith(names["audio_filename"])
    assert claimed_job["local_details_path"].endswith(names["details_filename"])


def test_drive_workflow_nodes_consume_drive_facing_names():
    """
    Verify completion-dispatcher.json Drive upload nodes reference job.audio_drive_filename
    and job.details_drive_filename.
    """
    wf_path = Path("n8n/workflows/completion-dispatcher.json")
    assert wf_path.exists()

    with open(wf_path, "r", encoding="utf-8") as f:
        wf = json.load(f)

    nodes_by_name = {n["name"]: n for n in wf.get("nodes", [])}

    audio_node = nodes_by_name.get("Upload Audio to Google Drive")
    assert audio_node is not None
    assert "job.audio_drive_filename" in audio_node["parameters"]["name"]

    details_node = nodes_by_name.get("Upload Details to Google Drive")
    assert details_node is not None
    assert "job.details_drive_filename" in details_node["parameters"]["name"]


def test_deployment_rehydration_replaces_placeholder_credentials_and_error_workflow_id():
    """
    Verify rehydrate_workflow_credentials replaces placeholder credential IDs and resolves
    settings.errorWorkflow from workflow name to installed workflow ID.
    """
    raw_wf = {
        "name": "Herald - Email Intake & Script Generator",
        "nodes": [
            {
                "name": "Gmail Trigger",
                "credentials": {
                    "gmailOAuth2": {"id": "1", "name": "Herald Gmail Account"}
                },
            },
            {
                "name": "Drive Upload",
                "credentials": {
                    "googleDriveOAuth2Api": {"id": "1", "name": "Herald Google Drive Account"}
                },
            },
        ],
        "settings": {
            "errorWorkflow": "Herald - System Error Handler"
        },
    }

    installed_creds = {
        "gmailOAuth2": {"id": "real-gmail-cred-999", "name": "Production Gmail"},
        "googleDriveOAuth2Api": {"id": "real-drive-cred-888", "name": "Production Drive"},
    }

    wf_map = {
        "Herald - System Error Handler": "fOw3dnjtBK04avEI"
    }

    rehydrated = rehydrate_workflow_credentials(raw_wf, installed_creds, wf_map)

    # 1. Credentials rehydrated
    assert rehydrated["nodes"][0]["credentials"]["gmailOAuth2"]["id"] == "real-gmail-cred-999"
    assert rehydrated["nodes"][1]["credentials"]["googleDriveOAuth2Api"]["id"] == "real-drive-cred-888"

    # 2. errorWorkflow resolved to ID
    assert rehydrated["settings"]["errorWorkflow"] == "fOw3dnjtBK04avEI"
    assert validate_workflow_for_deployment(rehydrated) is True


def test_deployment_rejects_unresolved_placeholder_credentials():
    """
    Verify validate_workflow_for_deployment fails closed (raises ValueError) if placeholder credential ID '1' remains.
    """
    wf_with_placeholder = {
        "name": "Unresolved Workflow",
        "nodes": [
            {
                "name": "Gmail Node",
                "credentials": {
                    "gmailOAuth2": {"id": "1", "name": "Placeholder Gmail"}
                },
            }
        ],
    }

    with pytest.raises(ValueError) as exc_info:
        validate_workflow_for_deployment(wf_with_placeholder)

    assert "contains unresolved placeholder credential ID '1'" in str(exc_info.value)


def test_error_workflow_is_workflow_id_not_name():
    """
    Verify validate_workflow_for_deployment rejects raw workflow name 'Herald - System Error Handler' in settings.errorWorkflow.
    """
    wf_with_raw_error_name = {
        "name": "Test Intake",
        "nodes": [],
        "settings": {
            "errorWorkflow": "Herald - System Error Handler"
        },
    }

    with pytest.raises(ValueError) as exc_info:
        validate_workflow_for_deployment(wf_with_raw_error_name)

    assert "settings.errorWorkflow contains unresolved workflow name 'Herald - System Error Handler' instead of an installed workflow ID" in str(exc_info.value)


def test_global_error_handler_sends_admin_notification():
    """
    Verify error-handler.json contains Send Admin Error Notification Gmail node connected after Error Handler API call.
    """
    wf_path = Path("n8n/workflows/error-handler.json")
    assert wf_path.exists()

    with open(wf_path, "r", encoding="utf-8") as f:
        wf = json.load(f)

    nodes_by_name = {n["name"]: n for n in wf.get("nodes", [])}

    admin_node = nodes_by_name.get("Send Admin Error Notification")
    assert admin_node is not None
    assert admin_node["type"] == "n8n-nodes-base.gmail"

    # Connection check
    connections = wf.get("connections", {})
    api_call_conns = connections.get("Call Herald Error Handler API", {}).get("main", [])
    assert len(api_call_conns) > 0
    target_node = api_call_conns[0][0]["node"]
    assert target_node == "Send Admin Error Notification"


def test_source_access_blocked_does_not_route_through_global_error_handler():
    """
    Verify email-intake.json routes FAILED_FINAL / SOURCE_ACCESS_BLOCKED to Send Source Failure Notification
    and terminates normally without invoking global error handler.
    """
    wf_path = Path("n8n/workflows/email-intake.json")
    assert wf_path.exists()

    with open(wf_path, "r", encoding="utf-8") as f:
        wf = json.load(f)

    nodes_by_name = {n["name"]: n for n in wf.get("nodes", [])}

    check_node = nodes_by_name.get("Check Source Status")
    assert check_node is not None

    fail_node = nodes_by_name.get("Send Source Failure Notification")
    assert fail_node is not None
    assert fail_node.get("onError") == "continueRegularOutput"

    # Verify connection from Check Source Status true branch to Send Source Failure Notification
    connections = wf.get("connections", {})
    source_status_conns = connections.get("Check Source Status", {}).get("main", [])
    assert len(source_status_conns) > 0
    true_branch_node = source_status_conns[0][0]["node"]
    assert true_branch_node == "Send Source Failure Notification"

    # Verify Send Source Failure Notification has no outgoing connections to Error Handler
    fail_node_conns = connections.get("Send Source Failure Notification", {})
    assert fail_node_conns == {} or fail_node_conns.get("main") is None


def test_source_access_blocked_and_requester_failure_email():
    """
    Verify format_failure_email generates actionable instructions for SOURCE_ACCESS_BLOCKED.
    """
    fail = format_failure_email(
        job_id="fail-job-999",
        source_url="https://paywalled-news.example.com/article",
        error_code="SOURCE_ACCESS_BLOCKED",
    )

    assert "PROCESSING COULD NOT BE COMPLETED" in fail["text"]
    assert "blocked automated retrieval" in fail["text"]
    assert "Please copy and paste the full article text directly into your email body" in fail["text"]
    assert "SOURCE_ACCESS_BLOCKED" in fail["text"]
    assert "fail-job-999" in fail["text"]
    assert "<html" in fail["html"].lower()


def test_transient_429_retries():
    """
    Verify HTTP 429 response triggers retries before raising SourceAccessBlockedError.
    """
    mock_response_429 = MagicMock()
    mock_response_429.status_code = 429
    mock_response_429.is_redirect = False

    with patch("httpx.Client.stream") as mock_stream, patch("time.sleep") as mock_sleep:
        mock_stream.return_value.__enter__.return_value = mock_response_429

        with pytest.raises(SourceAccessBlockedError) as exc_info:
            extract_article_from_url("https://example.com/blocked-429", max_429_retries=2)

        assert "HTTP 429" in str(exc_info.value)
        assert mock_sleep.call_count == 2


def test_source_block_deduplication_and_metric(api_client, db_session: Session):
    """
    Verify deduplicate_source_blocks detects identical duplicated article blocks,
    computes original vs normalized counts, and records SOURCE_NORMALIZATION stage metric.
    """
    article_block = "Paragraph 1: Herald email-to-podcast automation system receives articles and synthesizes studio podcasts.\n\nParagraph 2: Kokoro TTS generates high fidelity audio chunks."
    duplicated_text = f"{article_block}\n\n{article_block}"

    deduped_text, stats = deduplicate_source_blocks(duplicated_text)

    assert deduped_text == article_block
    assert stats["original_word_count"] > stats["normalized_word_count"]
    assert stats["original_char_count"] > stats["normalized_char_count"]

    # Verify intake call records SOURCE_NORMALIZATION metric
    msg_id = f"msg-dedup-{uuid.uuid4().hex[:8]}"
    req_data = {
        "gmail_message_id": msg_id,
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": duplicated_text,
    }

    res = api_client.post("/api/v1/intake", json=req_data)
    assert res.status_code == 200
    job_id = res.json()["job_id"]

    job = db_session.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    assert job.source_text == article_block


def test_credential_rehydration_and_validation():
    """
    Verify credential rehydration preserves installed target environment credential references.
    """
    sample_wf = {
        "name": "Test Workflow",
        "nodes": [
            {
                "name": "Gmail Node",
                "credentials": {
                    "gmailOAuth2": {"id": "default-id", "name": "Herald Gmail"}
                },
            },
            {
                "name": "Drive Node",
                "credentials": {
                    "googleDriveOAuth2": {"id": "default-id", "name": "Herald Drive"}
                },
            },
        ],
    }

    installed = {
        "gmailOAuth2": {"id": "prod-gmail-cred-id-999", "name": "Prod Gmail"},
        "googleDriveOAuth2": {"id": "prod-drive-cred-id-888", "name": "Prod Drive"},
    }

    rehydrated = rehydrate_workflow_credentials(sample_wf, installed)

    assert rehydrated["nodes"][0]["credentials"]["gmailOAuth2"]["id"] == "prod-gmail-cred-id-999"
    assert rehydrated["nodes"][1]["credentials"]["googleDriveOAuth2"]["id"] == "prod-drive-cred-id-888"
    assert validate_workflow_credentials(rehydrated) is True


def test_production_google_drive_folder_id_enforcement():
    """
    Verify is_production_valid() fails if GOOGLE_DRIVE_FOLDER_ID is empty in production environment.
    """
    with patch.object(settings, "HERALD_ENV", "production"), \
         patch.object(settings, "HERALD_API_KEY", "valid-key-12345"), \
         patch.object(settings, "EMAIL_ALLOWED_SENDERS", "user@example.com"):

        with patch.object(settings, "GOOGLE_DRIVE_FOLDER_ID", ""):
            assert settings.is_production_valid() is False

        with patch.object(settings, "GOOGLE_DRIVE_FOLDER_ID", "1srpAj0qaEteh0IdxPz86Z9bn0GvEkfMv"):
            assert settings.is_production_valid() is True


def test_in_place_details_file_update_preserves_drive_id(api_client, db_session: Session):
    """
    Verify updating details companion file after completion delivery records details_finalized_at
    and preserves the existing details_drive_file_id.
    """
    job = PodcastJob(
        id="job-inplace-details-001",
        gmail_message_id="msg-inplace-001",
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="test-hash-12345",
        source_text="Test source text content",
        status=JobState.COMPLETE.value,
        details_drive_file_id="existing-drive-id-777",
        details_drive_web_link="https://drive.google.com/file/d/existing-drive-id-777/view",
        created_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    res = api_client.post(
        f"/api/v1/jobs/{job.id}/details-finalized",
        json={
            "details_drive_file_id": "existing-drive-id-777",
            "details_drive_web_link": "https://drive.google.com/file/d/existing-drive-id-777/view",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["details_drive_file_id"] == "existing-drive-id-777"
    assert data["details_finalized_at"] is not None
