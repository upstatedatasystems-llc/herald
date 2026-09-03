import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.telegram_bot.main import TELEGRAM_BOT_COMMANDS
from herald.config import settings
from herald.db.models import Base, PodcastJob
from herald.telegram.auth import (
    generate_pairing_code,
    get_effective_user_preferences,
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
from herald.telegram.client import TelegramAPIError, TelegramClient


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


def test_telegram_modules_import_cleanly():
    """Smoke test proving Cycle 1 Telegram modules import successfully without NameError or missing Any."""
    import apps.telegram_bot.main
    import herald.telegram.auth
    import herald.telegram.bot
    import herald.telegram.client
    import herald.telegram.formatters
    import herald.telegram.pairing_cli

    assert apps.telegram_bot.main is not None
    assert herald.telegram.auth is not None
    assert herald.telegram.bot is not None
    assert herald.telegram.client is not None
    assert herald.telegram.formatters is not None
    assert herald.telegram.pairing_cli is not None


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

    # 3. Paired owner from WRONG private chat ID -> rejected
    mock_client.reset_mock()
    cb_wrong_chat = {
        "id": "cb-203",
        "from": {"id": 12345, "username": "owner"},
        "message": {"message_id": 10, "chat": {"id": 54321, "type": "private"}},
        "data": "h2:settings:confirm:on",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_wrong_chat)
    mock_client.answer_callback_query.assert_called_with("cb-203", text="Unauthorized: Access denied.", show_alert=True)


def test_callback_answers_before_message_edit(db_session):
    """Test that answerCallbackQuery is called BEFORE editMessageText network I/O."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    call_order = []
    mock_client = MagicMock(spec=TelegramClient)
    mock_client.answer_callback_query.side_effect = lambda *args, **kwargs: call_order.append("answerCallbackQuery")
    mock_client.edit_message_text.side_effect = lambda *args, **kwargs: call_order.append("editMessageText")

    cb_on = {
        "id": "cb-order-1",
        "from": {"id": 12345, "username": "owner"},
        "message": {"message_id": 50, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:settings:confirm:on",
    }

    handle_telegram_callback_query(db_session, mock_client, cb_on)

    assert call_order == ["answerCallbackQuery", "editMessageText"]


def test_callback_edit_message_text_error_handling(db_session):
    """Test that 'message is not modified' is benign debug, while other edit errors log warnings."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)

    cb = {
        "id": "cb-err-1",
        "from": {"id": 12345, "username": "owner"},
        "message": {"message_id": 50, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:settings:confirm:on",
    }

    # 1. Benign "message is not modified" -> logs debug, not warning
    mock_client.edit_message_text.side_effect = TelegramAPIError("Bad Request: message is not modified")
    with patch("herald.telegram.bot.logger.warning") as mock_warn, patch("herald.telegram.bot.logger.debug") as mock_debug:
        handle_telegram_callback_query(db_session, mock_client, cb)
        mock_warn.assert_not_called()
        mock_debug.assert_called()

    # 2. Real TelegramAPIError (e.g. Chat not found or Network error) -> logs warning
    mock_client.edit_message_text.side_effect = TelegramAPIError("Forbidden: bot was blocked by the user")
    with patch("herald.telegram.bot.logger.warning") as mock_warn, patch("herald.telegram.bot.logger.debug") as mock_debug:
        handle_telegram_callback_query(db_session, mock_client, cb)
        mock_warn.assert_called_once()
        assert "Failed to update settings message markup" in mock_warn.call_args[0][0]


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


def test_typed_preference_setters(db_session, monkeypatch):
    """Test strictly typed preference setters with input validation."""
    monkeypatch.setattr(settings, "AI_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "dummy_key")

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
    """Test that /settings, /voices, and /download are registered in TELEGRAM_BOT_COMMANDS."""
    registered_names = {cmd["command"] for cmd in TELEGRAM_BOT_COMMANDS}
    assert "settings" in registered_names
    assert "voices" in registered_names
    assert "download" in registered_names
    assert {"start", "help", "status", "ai_check", "queue", "settings", "readme", "voices", "download"}.issubset(registered_names)


def test_send_audio_and_document_multipart_reply_markup_and_file_id(tmp_path, monkeypatch):
    """Test send_audio and send_document: multipart JSON string serialization vs explicit file_id."""
    client = TelegramClient(token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")

    posted_calls = []

    def mock_post(url, *args, **kwargs):
        posted_calls.append({"url": url, "data": kwargs.get("data"), "json": kwargs.get("json"), "files": kwargs.get("files")})
        return MagicMock(json=lambda: {"ok": True, "result": {"message_id": 200}})

    monkeypatch.setattr("httpx.Client.post", mock_post)

    test_markup = {"inline_keyboard": [[{"text": "Btn", "callback_data": "h2:test"}]]}

    # 1. Local audio file with reply_markup -> sends multipart with JSON string reply_markup
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"dummy audio")
    client.send_audio(chat_id=123, audio_path=audio_file, reply_markup=test_markup)

    assert len(posted_calls) == 1
    call1 = posted_calls[0]
    assert call1["files"] is not None
    assert call1["data"]["reply_markup"] == json.dumps(test_markup)

    # 2. Explicit file_id audio with reply_markup -> sends JSON payload with dict reply_markup
    client.send_audio(chat_id=123, file_id="CQADBAADbAIAAjZ_uVB", reply_markup=test_markup)
    assert len(posted_calls) == 2
    call2 = posted_calls[1]
    assert call2["files"] is None
    assert call2["json"]["audio"] == "CQADBAADbAIAAjZ_uVB"
    assert call2["json"]["reply_markup"] == test_markup

    # 3. Nonexistent audio_path -> raises FileNotFoundError
    with pytest.raises(FileNotFoundError, match="does not exist"):
        client.send_audio(chat_id=123, audio_path=tmp_path / "nonexistent.mp3")

    # 4. Local document file with reply_markup -> sends multipart with JSON string reply_markup
    doc_file = tmp_path / "doc.pdf"
    doc_file.write_bytes(b"dummy pdf")
    client.send_document(chat_id=123, document_path=doc_file, reply_markup=test_markup)
    assert len(posted_calls) == 3
    call3 = posted_calls[2]
    assert call3["files"] is not None
    assert call3["data"]["reply_markup"] == json.dumps(test_markup)

    # 5. Explicit file_id document with reply_markup -> sends JSON payload with dict reply_markup
    client.send_document(chat_id=123, file_id="BQADBAADbAIAAjZ_uVD", reply_markup=test_markup)
    assert len(posted_calls) == 4
    call4 = posted_calls[3]
    assert call4["files"] is None
    assert call4["json"]["document"] == "BQADBAADbAIAAjZ_uVD"
    assert call4["json"]["reply_markup"] == test_markup

    # 6. Nonexistent document_path -> raises FileNotFoundError
    with pytest.raises(FileNotFoundError, match="does not exist"):
        client.send_document(chat_id=123, document_path=tmp_path / "nonexistent.doc")
