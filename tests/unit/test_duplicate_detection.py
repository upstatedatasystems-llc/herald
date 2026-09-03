import uuid
from datetime import UTC, datetime

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


def test_exact_duplicate_rejected_and_reused(api_client, db_session: Session):
    """1. Exact same source + settings returned as duplicate with same job_id."""
    req_data = {
        "gmail_message_id": "msg-dup-1",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": "This is sample article content for testing duplicate detection exact match logic.",
    }

    r1 = api_client.post("/api/v1/intake", json=req_data)
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["is_duplicate"] is False
    job_id_1 = d1["job_id"]

    req_data_2 = {
        "gmail_message_id": "msg-dup-2",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": "This is sample article content for testing duplicate detection exact match logic.",
    }

    r2 = api_client.post("/api/v1/intake", json=req_data_2)
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["is_duplicate"] is True
    assert d2["job_id"] == job_id_1


def test_same_source_different_mode_creates_new_job(api_client, db_session: Session):
    """2. Same source + different request_mode creates a NEW job."""
    body = "Unique source text paragraph for mode variation testing in herald duplicate detection."

    # First request: Brief mode
    r1 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": "msg-mode-1",
        "sender_email": "user@example.com",
        "subject": "Podcast: Brief",
        "body_text": body,
    })
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["is_duplicate"] is False
    assert d1["request_mode"] == "brief"

    # Second request: Standard mode
    r2 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": "msg-mode-2",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": body,
    })
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["is_duplicate"] is False
    assert d2["request_mode"] == "standard"
    assert d2["job_id"] != d1["job_id"]


def test_same_source_different_research_depth_creates_new_job(api_client, db_session: Session):
    """3. Same source + different research_depth creates a NEW job."""
    body = "Deep investigation content for testing research depth deduplication handling."

    r1 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": "msg-rdepth-1",
        "sender_email": "user@example.com",
        "subject": "Podcast: Research Low",
        "body_text": body,
    })
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["is_duplicate"] is False

    r2 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": "msg-rdepth-2",
        "sender_email": "user@example.com",
        "subject": "Podcast: Research High",
        "body_text": body,
    })
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["is_duplicate"] is False
    assert d2["job_id"] != d1["job_id"]


def test_same_source_different_voice_creates_new_job(api_client, db_session: Session):
    """4. Same source + different voice directive creates a NEW job."""
    body1 = "Voice: af_heart\n\nArticle body paragraph for voice testing."
    body2 = "Voice: am_adam\n\nArticle body paragraph for voice testing."

    r1 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": "msg-voice-1",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": body1,
    })
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["is_duplicate"] is False

    r2 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": "msg-voice-2",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": body2,
    })
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["is_duplicate"] is False
    assert d2["job_id"] != d1["job_id"]


def test_same_source_different_speed_creates_new_job(api_client, db_session: Session):
    """5. Same source + different speed directive creates a NEW job."""
    body1 = "Speed: 1.0\n\nArticle text paragraph for speed testing."
    body2 = "Speed: 1.2\n\nArticle text paragraph for speed testing."

    r1 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": "msg-speed-1",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": body1,
    })
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["is_duplicate"] is False

    r2 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": "msg-speed-2",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": body2,
    })
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["is_duplicate"] is False
    assert d2["job_id"] != d1["job_id"]


def test_gmail_message_id_idempotency(api_client, db_session: Session):
    """6. Replaying exact same Gmail message ID returns existing job even if parameters differ."""
    msg_id = "msg-unique-gmail-id-999"

    r1 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": msg_id,
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": "Original message body content.",
    })
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["is_duplicate"] is False

    # Replay exact same message_id with different subject/body
    r2 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": msg_id,
        "sender_email": "user@example.com",
        "subject": "Podcast: Brief",
        "body_text": "Replayed email body text.",
    })
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["is_duplicate"] is True
    assert d2["job_id"] == d1["job_id"]


def test_concurrent_identical_intake_deduplication(api_client, db_session: Session):
    """7. Concurrent identical intake returns existing job cleanly without duplicate job creation."""
    msg_id = "msg-concurrent-777"
    body = "Concurrent intake test content paragraph."

    # Create job in database directly
    job = PodcastJob(
        id=str(uuid.uuid4()),
        gmail_message_id=msg_id,
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-conc-777",
        source_text=body,
        status=JobState.RECEIVED.value,
        created_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    # Intake request with same message_id
    r = api_client.post("/api/v1/intake", json={
        "gmail_message_id": msg_id,
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": body,
    })
    assert r.status_code == 200
    d = r.json()
    assert d["is_duplicate"] is True
    assert d["job_id"] == job.id


def test_same_source_different_chunk_size_creates_new_job(api_client, db_session: Session):
    """8. Same source + different chunk-N setting creates a NEW job with identical source_hash."""
    body = "Article body text for testing chunk size deduplication matching logic."

    r1 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": "msg-chunk-1",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard chunk-500",
        "body_text": body,
    })
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["is_duplicate"] is False

    r2 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": "msg-chunk-2",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard chunk-1000",
        "body_text": body,
    })
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["is_duplicate"] is False
    assert d2["job_id"] != d1["job_id"]

    # Verify source_hash is identical on both jobs in DB
    j1 = db_session.query(PodcastJob).filter(PodcastJob.id == d1["job_id"]).first()
    j2 = db_session.query(PodcastJob).filter(PodcastJob.id == d2["job_id"]).first()
    assert j1.source_hash == j2.source_hash
    assert j1.tts_chunk_chars == 500
    assert j2.tts_chunk_chars == 1000


def test_same_source_different_verify_creates_new_job(api_client, db_session: Session):
    """9. Same source + differing verify setting creates a NEW job with identical source_hash."""
    body = "Article body text for testing verify directive deduplication matching logic."

    r1 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": "msg-verify-1",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard",
        "body_text": body,
    })
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["is_duplicate"] is False

    r2 = api_client.post("/api/v1/intake", json={
        "gmail_message_id": "msg-verify-2",
        "sender_email": "user@example.com",
        "subject": "Podcast: Standard verify",
        "body_text": body,
    })
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["is_duplicate"] is False
    assert d2["job_id"] != d1["job_id"]

    j1 = db_session.query(PodcastJob).filter(PodcastJob.id == d1["job_id"]).first()
    j2 = db_session.query(PodcastJob).filter(PodcastJob.id == d2["job_id"]).first()
    assert j1.source_hash == j2.source_hash
    assert j1.verify_final_script is False
    assert j2.verify_final_script is True

