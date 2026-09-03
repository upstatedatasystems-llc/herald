from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.telegram_bot.main import TELEGRAM_BOT_COMMANDS
from herald.config import settings
from herald.db.models import Base, PodcastJob, TelegramPairingCode, TelegramUser
from herald.telegram.auth import (
    generate_pairing_code,
    get_effective_user_preferences,
    set_user_confirm_before_tts,
    set_user_default_mode,
    set_user_default_speed,
    set_user_default_voice,
    verify_and_claim_pairing_code,
)
from herald.telegram.bot import (
    handle_telegram_callback_query,
    handle_telegram_command,
    process_telegram_update,
)
from herald.telegram.client import TelegramClient
from herald.telegram.formatters import format_settings


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


def test_callback_byte_length_limit(db_session):
    """Test that callback data is strictly validated against the 64 UTF-8 byte limit."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)

    # 1. 64 ASCII bytes: valid length (under/equal to 64 bytes)
    valid_64_ascii = "a" * 64
    cb_valid = {
        "id": "cb-101",
        "from": {"id": 12345, "username": "owner"},
        "message": {"message_id": 10, "chat": {"id": 12345, "type": "private"}},
        "data": valid_64_ascii,
    }
    handle_telegram_callback_query(db_session, mock_client, cb_valid)
    mock_client.answer_callback_query.assert_called_with("cb-101", text="Action received.")

    # 2. 65 ASCII bytes: oversized -> rejected
    mock_client.reset_mock()
    oversized_65_ascii = "a" * 65
    cb_oversized = {
        "id": "cb-102",
        "from": {"id": 12345, "username": "owner"},
        "message": {"message_id": 10, "chat": {"id": 12345, "type": "private"}},
        "data": oversized_65_ascii,
    }
    handle_telegram_callback_query(db_session, mock_client, cb_oversized)
    mock_client.answer_callback_query.assert_called_with("cb-102", text="Error: Callback data too large.", show_alert=True)

    # 3. Multibyte string: character count <= 64 but byte length > 64 bytes -> rejected
    mock_client.reset_mock()
    multibyte_str = "🎙️" * 20  # 4 bytes each = 80 bytes
    assert len(multibyte_str) < 64
    assert len(multibyte_str.encode("utf-8")) > 64
    cb_multibyte = {
        "id": "cb-103",
        "from": {"id": 12345, "username": "owner"},
        "message": {"message_id": 10, "chat": {"id": 12345, "type": "private"}},
        "data": multibyte_str,
    }
    handle_telegram_callback_query(db_session, mock_client, cb_multibyte)
    mock_client.answer_callback_query.assert_called_with("cb-103", text="Error: Callback data too large.", show_alert=True)


def test_callback_private_chat_and_auth_enforcement(db_session):
    """Test callback authorization & private chat context guards."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)

    # 1. Group chat context -> rejected
    cb_group = {
        "id": "cb-201",
        "from": {"id": 12345, "username": "owner"},
        "message": {"message_id": 10, "chat": {"id": -99999, "type": "group"}},
        "data": "h2:settings:confirm:on",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_group)
    mock_client.answer_callback_query.assert_called_with("cb-201", text="Herald operates only in private chats.", show_alert=True)

    # 2. Non-owner user -> rejected
    mock_client.reset_mock()
    cb_unauth = {
        "id": "cb-202",
        "from": {"id": 88888, "username": "intruder"},
        "message": {"message_id": 10, "chat": {"id": 88888, "type": "private"}},
        "data": "h2:settings:confirm:on",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_unauth)
    mock_client.answer_callback_query.assert_called_with("cb-202", text="Unauthorized: Access denied.", show_alert=True)


def test_callback_idempotency_set_semantics(db_session):
    """
    Test SET-semantics idempotency:
    - Calling :on twice leaves setting ON.
    - Calling :off twice leaves setting OFF.
    """
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)

    cb_on = {
        "id": "cb-on-1",
        "from": {"id": 12345, "username": "owner"},
        "message": {"message_id": 50, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:settings:confirm:on",
    }

    # First :on click
    handle_telegram_callback_query(db_session, mock_client, cb_on)
    prefs1 = get_effective_user_preferences(db_session, 12345)
    assert prefs1["confirm_before_tts"] is True
    mock_client.answer_callback_query.assert_called_with("cb-on-1", text="Confirm Before TTS enabled.")

    # Second :on click (repeated/double click)
    mock_client.reset_mock()
    cb_on["id"] = "cb-on-2"
    handle_telegram_callback_query(db_session, mock_client, cb_on)
    prefs2 = get_effective_user_preferences(db_session, 12345)
    assert prefs2["confirm_before_tts"] is True
    mock_client.answer_callback_query.assert_called_with("cb-on-2", text="Confirm Before TTS enabled.")

    # First :off click
    mock_client.reset_mock()
    cb_off = {
        "id": "cb-off-1",
        "from": {"id": 12345, "username": "owner"},
        "message": {"message_id": 50, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:settings:confirm:off",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_off)
    prefs3 = get_effective_user_preferences(db_session, 12345)
    assert prefs3["confirm_before_tts"] is False
    mock_client.answer_callback_query.assert_called_with("cb-off-1", text="Confirm Before TTS disabled.")

    # Second :off click (repeated/double click)
    mock_client.reset_mock()
    cb_off["id"] = "cb-off-2"
    handle_telegram_callback_query(db_session, mock_client, cb_off)
    prefs4 = get_effective_user_preferences(db_session, 12345)
    assert prefs4["confirm_before_tts"] is False
    mock_client.answer_callback_query.assert_called_with("cb-off-2", text="Confirm Before TTS disabled.")


def test_edited_message_idempotency(db_session):
    """Test that editing an already-submitted message does not duplicate jobs or crash polling."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)

    # 1. Existing job created from message_id 100
    job = PodcastJob(
        id="job-existing-100",
        transport="telegram",
        telegram_chat_id=12345,
        telegram_message_id=100,
        telegram_user_id=12345,
        status="COMPLETE",
        source_hash="hash100",
        source_text="Initial text",
    )
    db_session.add(job)
    db_session.commit()

    # 2. Receive edited_message update for message_id 100
    update = {
        "update_id": 999,
        "edited_message": {
            "message_id": 100,
            "from": {"id": 12345, "username": "owner"},
            "chat": {"id": 12345, "type": "private"},
            "text": "Edited text content",
        },
    }

    # Process update
    process_telegram_update(db_session, mock_client, update)

    # Invariant: No new jobs created
    assert db_session.query(PodcastJob).count() == 1


def test_settings_command_and_formatting(db_session):
    """Test /settings command displays preferences and provides inline toggle button."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)
    msg = {
        "message_id": 7,
        "from": {"id": 12345, "username": "owner"},
        "chat": {"id": 12345, "type": "private"},
        "text": "/settings",
    }

    handle_telegram_command(db_session, mock_client, msg, "settings", "")

    mock_client.send_message.assert_called_once()
    call_kwargs = mock_client.send_message.call_args[1]

    text = call_kwargs["text"]
    markup = call_kwargs["reply_markup"]

    assert "Herald Preferences & Settings" in text
    assert "Confirm Before TTS:" in text
    assert "⚪ Off" in text
    assert markup is not None
    assert "inline_keyboard" in markup
    assert markup["inline_keyboard"][0][0]["callback_data"] == "h2:settings:confirm:on"


def test_typed_preference_setters(db_session):
    """Test strictly typed preference setters with input validation."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    # Voice
    set_user_default_voice(db_session, 12345, "af_bella")
    prefs = get_effective_user_preferences(db_session, 12345)
    assert prefs["default_voice"] == "af_bella"

    with pytest.raises(ValueError, match="not in allowed voices"):
        set_user_default_voice(db_session, 12345, "invalid_voice_xyz")

    # Speed
    set_user_default_speed(db_session, 12345, 1.1)
    prefs = get_effective_user_preferences(db_session, 12345)
    assert prefs["default_speed"] == 1.1

    with pytest.raises(ValueError, match="Speed .* out of range"):
        set_user_default_speed(db_session, 12345, 2.5)

    # Mode
    set_user_default_mode(db_session, 12345, "brief")
    prefs = get_effective_user_preferences(db_session, 12345)
    assert prefs["default_mode"] == "brief"

    with pytest.raises(ValueError, match="Invalid mode"):
        set_user_default_mode(db_session, 12345, "unsupported_mode")


def test_set_my_commands_includes_settings():
    """Test that /settings is registered in TELEGRAM_BOT_COMMANDS after Package 2B."""
    registered_names = {cmd["command"] for cmd in TELEGRAM_BOT_COMMANDS}
    assert "settings" in registered_names
    assert registered_names == {"start", "help", "status", "ai_check", "queue", "settings", "readme"}
