"""
Unit tests for URL extraction durable job creation and failure diagnostics.
"""

from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.core.models import HeraldRequest
from herald.core.pipeline import process_herald_request
from herald.db.models import Base, JobState, PodcastJob
from herald.extraction.url_extractor import ArticleExtractionError, SSRFVulnerabilityError


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_extraction_failure_persists_durable_job_with_diagnostics(db_session):
    req = HeraldRequest(
        source_url="https://example.com/blocked-article",
        transport="telegram",
        delivery_target="12345",
        transport_message_id="999",
        requester_identity="telegram:12345",
    )

    with patch("herald.core.pipeline.extract_article_from_url", side_effect=ArticleExtractionError("Publisher blocked retrieval")):
        resp = process_herald_request(db_session, req)

    assert resp.status == JobState.FAILED_FINAL.value
    assert resp.job_id != ""
    assert resp.error_category == "EXTRACTION_FAILURE"

    # Query DB directly to verify persistence
    saved_job = db_session.query(PodcastJob).filter(PodcastJob.id == resp.job_id).first()
    assert saved_job is not None
    assert saved_job.status == JobState.FAILED_FINAL.value
    assert saved_job.failed_stage == "EXTRACTION"
    assert saved_job.error_code == "EXTRACTION_FAILURE"
    assert "Publisher blocked retrieval" in (saved_job.error_detail or "")
    assert saved_job.auto_diagnostics_json is not None
    assert len(saved_job.auto_diagnostics_json) >= 1
    assert saved_job.auto_diagnostics_json[0]["stage"] == "extraction"


def test_ssrf_extraction_failure_persists_durable_job(db_session):
    req = HeraldRequest(
        source_url="http://169.254.169.254/latest/meta-data",
        transport="telegram",
        delivery_target="12345",
        transport_message_id="1000",
        requester_identity="telegram:12345",
    )

    with patch("herald.core.pipeline.extract_article_from_url", side_effect=SSRFVulnerabilityError("Target host resolves to prohibited IP")):
        resp = process_herald_request(db_session, req)

    assert resp.status == JobState.FAILED_FINAL.value
    assert resp.job_id != ""
    assert resp.error_category == "SSRF_PROTECTION"

    saved_job = db_session.query(PodcastJob).filter(PodcastJob.id == resp.job_id).first()
    assert saved_job is not None
    assert saved_job.status == JobState.FAILED_FINAL.value
    assert saved_job.failed_stage == "EXTRACTION"
    assert saved_job.error_code == "SSRF_PROTECTION"
    assert saved_job.auto_diagnostics_json is not None


def test_extraction_success_updates_provisional_job(db_session):
    req = HeraldRequest(
        source_url="https://example.com/valid-article",
        transport="telegram",
        delivery_target="12345",
        transport_message_id="1001",
        requester_identity="telegram:12345",
        request_mode="literal",
    )

    fake_text = "This is a valid news article with sufficient content for a test podcast episode."
    with patch("herald.core.pipeline.extract_article_from_url", return_value=("Article Title", fake_text, "https://example.com/canonical")):
        resp = process_herald_request(db_session, req)

    assert resp.job_id != ""
    assert resp.status in (JobState.QUEUED_TTS.value, JobState.AWAITING_APPROVAL.value)

    saved_job = db_session.query(PodcastJob).filter(PodcastJob.id == resp.job_id).first()
    assert saved_job is not None
    assert fake_text in saved_job.source_text
    assert saved_job.source_url == "https://example.com/canonical"
    assert saved_job.failed_stage is None
