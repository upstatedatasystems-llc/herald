from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import Base, JobState, PodcastJob, TelegramUser
from herald.telegram.auth import (
    generate_pairing_code,
    get_effective_user_preferences,
    verify_and_claim_pairing_code,
)
from herald.telegram.bot import handle_telegram_command
from herald.telegram.client import TelegramClient


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


def test_stale_persisted_user_defaults_revalidation(db_session, monkeypatch):
    """
    Test that stale stored user preferences are safely revalidated against runtime settings:
    - Stale voice not in ALLOWED_VOICES -> falls back to instance default
    - Out of bounds speed -> falls back to 1.0
    - AI mode when AI is unconfigured -> falls back to literal
    """
    user = TelegramUser(
        telegram_user_id=12345,
        telegram_chat_id=12345,
        role="user",
        is_active=True,
        default_voice="nonexistent_old_voice",
        default_speed=2.5,  # Above MAX_SPEED
        default_mode="standard",
    )
    db_session.add(user)
    db_session.commit()

    # Case 1: AI configured
    monkeypatch.setattr(settings, "AI_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")
    monkeypatch.setattr(settings, "KOKORO_VOICE", "af_heart")
    monkeypatch.setattr(settings, "KOKORO_SPEED", 1.0)

    prefs = get_effective_user_preferences(db_session, 12345)
    assert prefs["default_voice"] == "af_heart"  # Stale voice replaced with instance default
    assert prefs["default_speed"] == 1.0  # Out of bounds speed replaced
    assert prefs["default_mode"] == "standard"

    # Case 2: AI not configured -> standard mode falls back to literal
    monkeypatch.setattr(settings, "AI_PROVIDER", None)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)

    prefs_no_ai = get_effective_user_preferences(db_session, 12345)
    assert prefs_no_ai["default_mode"] == "literal"


def test_queue_command_tenant_isolation(db_session):
    """
    /queue command isolates jobs for regular allowlisted users (only seeing their own jobs),
    while the paired owner has administrative visibility over all active jobs.
    """
    # 1. Pair owner (user 100)
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=100, chat_id=100, username="owner")

    # 2. Add regular allowlisted user (user 200)
    user2 = TelegramUser(telegram_user_id=200, telegram_chat_id=200, role="user", is_active=True)
    db_session.add(user2)

    # 3. Add jobs
    j_owner = PodcastJob(
        id="owner-job-1111-2222-3333-444444444444",
        transport="telegram",
        telegram_user_id=100,
        telegram_chat_id=100,
        status=JobState.QUEUED_TTS.value,
        custom_title="Owner Secret Episode",
        source_hash="h100",
        source_text="Secret owner text",
    )
    j_user = PodcastJob(
        id="user-job-1111-2222-3333-444444444444",
        transport="telegram",
        telegram_user_id=200,
        telegram_chat_id=200,
        status=JobState.SYNTHESIZING.value,
        custom_title="User Public Episode",
        source_hash="h200",
        source_text="Public user text",
    )
    db_session.add_all([j_owner, j_user])
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)

    # Regular user queries /queue
    msg_user = {"chat": {"id": 200, "type": "private"}, "from": {"id": 200}, "message_id": 1}
    handle_telegram_command(db_session, mock_client, msg_user, "queue", "")
    user_queue_text = mock_client.send_message.call_args[1]["text"]
    assert "User Public Episode" in user_queue_text
    assert "Owner Secret Episode" not in user_queue_text  # Owner's job is hidden from regular user!

    # Owner queries /queue -> sees both jobs
    mock_client.reset_mock()
    msg_owner = {"chat": {"id": 100, "type": "private"}, "from": {"id": 100}, "message_id": 2}
    handle_telegram_command(db_session, mock_client, msg_owner, "queue", "")
    owner_queue_text = mock_client.send_message.call_args[1]["text"]
    assert "User Public Episode" in owner_queue_text
    assert "Owner Secret Episode" in owner_queue_text


def test_status_command_counts_awaiting_approval_as_active(db_session):
    """
    /status active count includes AWAITING_APPROVAL and other nonterminal jobs.
    """
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=100, chat_id=100, username="owner")

    j_awaiting = PodcastJob(
        id="awaiting-job-1111-2222-3333-444444444444",
        transport="telegram",
        telegram_user_id=100,
        telegram_chat_id=100,
        status=JobState.AWAITING_APPROVAL.value,
        source_hash="ha",
        source_text="Awaiting text",
    )
    j_complete = PodcastJob(
        id="complete-job-1111-2222-3333-444444444444",
        transport="telegram",
        telegram_user_id=100,
        telegram_chat_id=100,
        status=JobState.COMPLETE.value,
        source_hash="hc",
        source_text="Complete text",
    )
    db_session.add_all([j_awaiting, j_complete])
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    msg = {"chat": {"id": 100, "type": "private"}, "from": {"id": 100}, "message_id": 1}
    handle_telegram_command(db_session, mock_client, msg, "status", "")

    status_text = mock_client.send_message.call_args[1]["text"]
    assert "• <b>Active Jobs:</b> 1" in status_text
    assert "• <b>Completed Jobs:</b> 1" in status_text
