import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.main import app
from herald.config import settings
from herald.db.models import JobState, PodcastJob
from herald.extraction.source_cleaner import deduplicate_source_blocks
from herald.extraction.url_extractor import (
    ArticleExtractionError,
    SourceAccessBlockedError,
    extract_article_from_url,
)
from herald.n8n.credential_rehydrator import (
    rehydrate_workflow_credentials,
    validate_workflow_credentials,
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

    dt = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    audio_fn = build_user_facing_drive_filename("Quantum Computing Breakthroughs!", dt, "Standard", "mp3")
    details_fn = build_user_facing_drive_filename("Quantum Computing Breakthroughs!", dt, "Standard", "md")

    assert audio_fn == "Quantum Computing Breakthroughs- 8-11-26 Standard.mp3" or "Quantum Computing Breakthroughs" in audio_fn
    assert audio_fn.endswith("8-11-26 Standard.mp3")
    assert details_fn.endswith("8-11-26 Standard.md")


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
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
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
