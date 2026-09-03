from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.db.models import Base, JobState, PodcastJob
from herald.telegram.auth import generate_pairing_code, verify_and_claim_pairing_code
from herald.telegram.bot import handle_telegram_callback_query, handle_telegram_command
from herald.telegram.client import TelegramClient
from herald.telegram.formatters import format_completion_markup
from herald.telegram.resolver import resolve_user_job


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_resolve_user_job_latest_and_by_id(db_session):
    """resolve_user_job correctly resolves user's latest job, exact UUID, and short prefix."""
    now = datetime.now(UTC)
    user_id = 12345
    chat_id = 12345

    # 1. Older completed job
    j1 = PodcastJob(
        id="11111111-2222-3333-4444-555555555555",
        transport="telegram",
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        status=JobState.COMPLETE.value,
        source_hash="h1",
        source_text="Test source 1",
        created_at=now - timedelta(hours=2),
        completed_at=now - timedelta(hours=2),
    )
    # 2. Newer completed job
    j2 = PodcastJob(
        id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        transport="telegram",
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        status=JobState.COMPLETE.value,
        source_hash="h2",
        source_text="Test source 2",
        created_at=now - timedelta(minutes=10),
        completed_at=now - timedelta(minutes=5),
    )
    # 3. Another user's job
    j3 = PodcastJob(
        id="99999999-9999-9999-9999-999999999999",
        transport="telegram",
        telegram_user_id=99999,
        telegram_chat_id=99999,
        status=JobState.COMPLETE.value,
        source_hash="h3",
        source_text="Test source 3",
        created_at=now,
        completed_at=now,
    )
    db_session.add_all([j1, j2, j3])
    db_session.commit()

    # Default query (no arg) returns latest (j2)
    latest = resolve_user_job(db_session, user_id, chat_id)
    assert latest is not None
    assert latest.id == j2.id

    # Exact UUID query for older job (j1)
    exact = resolve_user_job(db_session, user_id, chat_id, "11111111-2222-3333-4444-555555555555")
    assert exact is not None
    assert exact.id == j1.id

    # Prefix query (min 4 chars)
    prefix = resolve_user_job(db_session, user_id, chat_id, "aaaaaaa")
    assert prefix is not None
    assert prefix.id == j2.id

    # Cross-tenant query for j3 is rejected
    cross = resolve_user_job(db_session, user_id, chat_id, "99999999-9999-9999-9999-999999999999")
    assert cross is None


def test_download_command_reuses_document_file_id(db_session):
    """/download reuses telegram_document_file_id when available without uploading disk file."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    job = PodcastJob(
        id="download-test-job-001",
        transport="telegram",
        telegram_user_id=12345,
        telegram_chat_id=12345,
        status=JobState.COMPLETE.value,
        source_hash="h1",
        source_text="Test source text",
        telegram_document_file_id="tg_doc_file_id_abc123",
        completed_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    msg = {
        "chat": {"id": 12345, "type": "private"},
        "from": {"id": 12345},
        "message_id": 401,
    }

    handle_telegram_command(db_session, mock_client, msg, "download", "")

    mock_client.send_document.assert_called_once()
    call_kwargs = mock_client.send_document.call_args[1]
    assert call_kwargs["file_id"] == "tg_doc_file_id_abc123"


def test_download_command_uploads_local_file_and_caches_file_id(db_session, tmp_path):
    """/download uploads local MP3 as document when document_file_id is absent, then persists it."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mp3_file = tmp_path / "test_ep.mp3"
    mp3_file.write_bytes(b"dummy mp3 data")

    job = PodcastJob(
        id="download-test-job-002",
        transport="telegram",
        telegram_user_id=12345,
        telegram_chat_id=12345,
        status=JobState.COMPLETE.value,
        source_hash="h2",
        source_text="Test source text",
        local_audio_path=str(mp3_file),
        completed_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.send_document.return_value = {
        "message_id": 999,
        "document": {"file_id": "newly_generated_doc_file_id_789"},
    }

    msg = {
        "chat": {"id": 12345, "type": "private"},
        "from": {"id": 12345},
        "message_id": 402,
    }

    handle_telegram_command(db_session, mock_client, msg, "download", "download-test-job-002")

    mock_client.send_document.assert_called_once()
    assert mock_client.send_document.call_args[1]["document_path"] == str(mp3_file)

    # Verify cached document_file_id in database
    db_session.refresh(job)
    assert job.telegram_document_file_id == "newly_generated_doc_file_id_789"


def test_download_callback_handles_request(db_session):
    """h2:download:<uuid> callback answers callback and sends document."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    job = PodcastJob(
        id="download-cb-job-001",
        transport="telegram",
        telegram_user_id=12345,
        telegram_chat_id=12345,
        status=JobState.COMPLETE.value,
        source_hash="h3",
        source_text="Test source text",
        telegram_document_file_id="doc_file_id_cb",
        completed_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    cb_query = {
        "id": "cb-dl-1",
        "from": {"id": 12345},
        "message": {"message_id": 501, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:download:download-cb-job-001",
    }

    handle_telegram_callback_query(db_session, mock_client, cb_query)

    mock_client.answer_callback_query.assert_called_with("cb-dl-1", text="Sending MP3 file...")
    mock_client.send_document.assert_called_once()
    assert mock_client.send_document.call_args[1]["file_id"] == "doc_file_id_cb"


def test_format_completion_markup_contains_download_button():
    """format_completion_markup provides [ 📥 Download MP3 ] callback button."""
    job = PodcastJob(id="job-uuid-download-btn-1")
    markup = format_completion_markup(job)

    assert "inline_keyboard" in markup
    btn = markup["inline_keyboard"][0][0]
    assert "Download MP3" in btn["text"]
    assert btn["callback_data"] == "h2:download:job-uuid-download-btn-1"
