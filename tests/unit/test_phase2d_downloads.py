from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.db.models import Base, JobState, PodcastJob
from herald.telegram.auth import generate_pairing_code, verify_and_claim_pairing_code
from herald.telegram.bot import handle_telegram_callback_query, handle_telegram_command
from herald.telegram.client import TelegramClient
from herald.telegram.delivery import deliver_job_download
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


def test_resolve_user_job_resolver_contract(db_session):
    """
    Test full approved resolve_user_job contract:
    - latest completed caller job
    - exact UUID match
    - unambiguous prefix match
    - ambiguous prefix rejected
    - completed_only flag filters out non-COMPLETE
    - wildcard / injection characters rejected
    - tenant isolation (cross-user invisible)
    """
    now = datetime.now(UTC)
    user_id = 12345
    chat_id = 12345

    # 1. Non-complete job (SYNTHESIZING)
    j_synth = PodcastJob(
        id="11111111-2222-3333-4444-000000000000",
        transport="telegram",
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        status=JobState.SYNTHESIZING.value,
        source_hash="h0",
        source_text="Test synth",
        created_at=now - timedelta(minutes=1),
    )
    # 2. Older completed job
    j_old = PodcastJob(
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
    # 3. Newer completed job (shares prefix "1111" with j_old!)
    j_ambig = PodcastJob(
        id="11111111-9999-3333-4444-888888888888",
        transport="telegram",
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        status=JobState.COMPLETE.value,
        source_hash="h2",
        source_text="Test source 2",
        created_at=now - timedelta(minutes=10),
        completed_at=now - timedelta(minutes=5),
    )
    # 4. Another user's job
    j_other = PodcastJob(
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
    db_session.add_all([j_synth, j_old, j_ambig, j_other])
    db_session.commit()

    # Default query with completed_only=True returns latest completed (j_ambig)
    latest_c = resolve_user_job(db_session, user_id, chat_id, completed_only=True)
    assert latest_c is not None
    assert latest_c.id == j_ambig.id

    # completed_only=True rejects explicit non-complete job
    non_c = resolve_user_job(db_session, user_id, chat_id, identifier="11111111-2222-3333-4444-000000000000", completed_only=True)
    assert non_c is None

    # Ambiguous prefix "11111111" (matches both j_old and j_ambig) -> returns None
    ambig_match = resolve_user_job(db_session, user_id, chat_id, identifier="11111111", completed_only=True)
    assert ambig_match is None

    # Unambiguous prefix "11111111-9999" -> matches j_ambig
    unique_match = resolve_user_job(db_session, user_id, chat_id, identifier="11111111-9999", completed_only=True)
    assert unique_match is not None
    assert unique_match.id == j_ambig.id

    # Wildcard and SQL injection characters rejected
    assert resolve_user_job(db_session, user_id, chat_id, identifier="%") is None
    assert resolve_user_job(db_session, user_id, chat_id, identifier="_") is None
    assert resolve_user_job(db_session, user_id, chat_id, identifier="1111%") is None
    assert resolve_user_job(db_session, user_id, chat_id, identifier="abc'; DROP TABLE") is None
    assert resolve_user_job(db_session, user_id, chat_id, identifier="12") is None  # Too short (< 4 chars)

    # Cross-tenant query rejected
    assert resolve_user_job(db_session, user_id, chat_id, identifier="99999999-9999-9999-9999-999999999999") is None


def test_deliver_job_download_priority_hierarchy(db_session, tmp_path):
    """
    Test delivery priority hierarchy in deliver_job_download:
    1. local MP3 exists -> sendDocument -> MIME audio/mpeg -> capture telegram_document_file_id
    2. local MP3 absent + telegram_document_file_id exists -> sendDocument(file_id=...)
    3. no document form + telegram_audio_file_id exists -> sendAudio(file_id=...)
    4. neither -> clear unavailable response
    """
    mock_client = MagicMock(spec=TelegramClient)

    # 1. Local file present
    local_mp3 = tmp_path / "ep1.mp3"
    local_mp3.write_bytes(b"mp3_data_bytes")
    job1 = PodcastJob(
        id="dl-prio-1111-2222-3333-444444444444",
        transport="telegram",
        telegram_user_id=123,
        telegram_chat_id=123,
        status=JobState.COMPLETE.value,
        local_audio_path=str(local_mp3),
        source_hash="p1",
        source_text="Test priority 1 text",
        custom_title="Priority One Episode",
    )
    db_session.add(job1)
    db_session.commit()

    mock_client.send_document.return_value = {
        "message_id": 101,
        "document": {"file_id": "doc_file_id_prio1"},
    }

    res1 = deliver_job_download(db_session, mock_client, job1, chat_id=123)
    assert res1 is True
    mock_client.send_document.assert_called_once()
    call1 = mock_client.send_document.call_args[1]
    assert call1["document_path"] == str(local_mp3)
    assert call1["parse_mode"] == "HTML"
    assert call1["mime_type"] == "audio/mpeg"
    assert "Priority One Episode" in call1["caption"]
    db_session.refresh(job1)
    assert job1.telegram_document_file_id == "doc_file_id_prio1"

    # 2. Local file absent + document_file_id present
    mock_client.reset_mock()
    job2 = PodcastJob(
        id="dl-prio-2222-2222-3333-444444444444",
        transport="telegram",
        telegram_user_id=123,
        telegram_chat_id=123,
        status=JobState.COMPLETE.value,
        telegram_document_file_id="cached_doc_id_222",
        source_hash="p2",
        source_text="Test priority 2 text",
    )
    db_session.add(job2)
    db_session.commit()

    res2 = deliver_job_download(db_session, mock_client, job2, chat_id=123)
    assert res2 is True
    mock_client.send_document.assert_called_once()
    call2 = mock_client.send_document.call_args[1]
    assert call2["file_id"] == "cached_doc_id_222"
    assert call2["parse_mode"] == "HTML"

    # 3. No document file + audio_file_id present -> sendAudio
    mock_client.reset_mock()
    job3 = PodcastJob(
        id="dl-prio-3333-2222-3333-444444444444",
        transport="telegram",
        telegram_user_id=123,
        telegram_chat_id=123,
        status=JobState.COMPLETE.value,
        telegram_audio_file_id="cached_audio_id_333",
        source_hash="p3",
        source_text="Test priority 3 text",
    )
    db_session.add(job3)
    db_session.commit()

    res3 = deliver_job_download(db_session, mock_client, job3, chat_id=123)
    assert res3 is True
    mock_client.send_audio.assert_called_once()
    call3 = mock_client.send_audio.call_args[1]
    assert call3["file_id"] == "cached_audio_id_333"
    assert call3["parse_mode"] == "HTML"

    # 4. Neither exists -> unavailable message
    mock_client.reset_mock()
    job4 = PodcastJob(
        id="dl-prio-4444-2222-3333-444444444444",
        transport="telegram",
        telegram_user_id=123,
        telegram_chat_id=123,
        status=JobState.COMPLETE.value,
        source_hash="p4",
        source_text="Test priority 4 text",
    )
    db_session.add(job4)
    db_session.commit()

    res4 = deliver_job_download(db_session, mock_client, job4, chat_id=123)
    assert res4 is False
    mock_client.send_message.assert_called_once()
    assert "Audio file is no longer available" in mock_client.send_message.call_args[1]["text"]


def test_download_command_and_callback_share_service(db_session, tmp_path):
    """Both /download command and h2:download callback route through deliver_job_download."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    local_file = tmp_path / "shared_dl.mp3"
    local_file.write_bytes(b"test_audio_bytes")

    job = PodcastJob(
        id="aaaaaaaa-1111-2222-3333-444444444444",
        transport="telegram",
        telegram_user_id=12345,
        telegram_chat_id=12345,
        status=JobState.COMPLETE.value,
        source_hash="sh1",
        source_text="Test shared dl source",
        local_audio_path=str(local_file),
        completed_at=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.send_document.return_value = {"message_id": 301, "document": {"file_id": "file_id_xyz"}}

    # Test /download command
    msg = {"chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}, "message_id": 10}
    handle_telegram_command(db_session, mock_client, msg, "download", "aaaaaaaa")
    mock_client.send_document.assert_called_once()
    assert mock_client.send_document.call_args[1]["document_path"] == str(local_file)

    # Test callback query
    mock_client.reset_mock()
    cb_query = {
        "id": "cb-dl-shared",
        "from": {"id": 12345},
        "message": {"message_id": 20, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:download:aaaaaaaa-1111-2222-3333-444444444444",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_query)
    mock_client.answer_callback_query.assert_called_with("cb-dl-shared", text="Sending MP3 file...")
    mock_client.send_document.assert_called_once()


def test_format_completion_markup_contains_download_button():
    """format_completion_markup provides [ 📥 Download MP3 ] callback button."""
    job = PodcastJob(id="job-uuid-download-btn-1")
    markup = format_completion_markup(job)

    assert "inline_keyboard" in markup
    btn = markup["inline_keyboard"][0][0]
    assert "Download MP3" in btn["text"]
    assert btn["callback_data"] == "h2:download:job-uuid-download-btn-1"
