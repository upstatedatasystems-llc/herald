from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.ai.catalog import get_model_token
from herald.db.models import Base, TelegramUser
from herald.telegram.auth import (
    generate_pairing_code,
    verify_and_claim_pairing_code,
)
from herald.telegram.bot import handle_telegram_callback_query, handle_telegram_command


def _setup_test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    db = TestingSession()
    code = generate_pairing_code(db)
    verify_and_claim_pairing_code(db, code, user_id=12345, chat_id=12345, username="owner")
    return db


def test_models_command_renders_catalog():
    db = _setup_test_db()
    mock_client = MagicMock()
    mock_client.is_configured = True

    msg = {
        "chat": {"id": 12345, "type": "private"},
        "from": {"id": 12345},
        "message_id": 10,
        "text": "/models",
    }
    handle_telegram_command(db, mock_client, msg, "models", "")

    mock_client.send_message.assert_called_once()
    text = mock_client.send_message.call_args[1]["text"]
    assert "Herald AI Models Catalog" in text
    assert "Gemini" in text
    assert "Groq" in text
    assert "Cloudflare" in text
    assert "OpenAI" in text
    assert "Literal" in text


def test_ai_check_command():
    db = _setup_test_db()
    mock_client = MagicMock()
    mock_client.is_configured = True

    msg = {
        "chat": {"id": 12345, "type": "private"},
        "from": {"id": 12345},
        "message_id": 11,
        "text": "/ai-check",
    }
    handle_telegram_command(db, mock_client, msg, "ai-check", "")

    assert mock_client.send_message.call_count >= 2
    final_card = mock_client.send_message.call_args_list[-1][1]["text"]
    assert "AI Provider Diagnostics" in final_card
    assert "Your Failover Chain:" in final_card


def test_ai_providers_menu_and_slot_selection():
    db = _setup_test_db()
    mock_client = MagicMock()

    with patch("herald.telegram.bot.is_provider_configured", return_value=True), \
         patch("herald.telegram.auth.is_provider_configured", return_value=True):
        cb = {
            "id": "cb1",
            "from": {"id": 12345},
            "message": {"message_id": 100, "chat": {"id": 12345, "type": "private"}},
            "data": "h3:settings:providers",
        }
        handle_telegram_callback_query(db, mock_client, cb)
        mock_client.edit_message_text.assert_called_once()
        text = mock_client.edit_message_text.call_args[1]["text"]
        assert "AI Provider Chain Configuration" in text

        mock_client.reset_mock()
        cb["data"] = "h3:p:slot:0"
        handle_telegram_callback_query(db, mock_client, cb)
        text = mock_client.edit_message_text.call_args[1]["text"]
        assert "Select Primary Provider" in text

        mock_client.reset_mock()
        cb["data"] = "h3:p:set:0:groq"
        handle_telegram_callback_query(db, mock_client, cb)

        user = db.query(TelegramUser).filter_by(telegram_user_id=12345).first()
        assert user.ai_provider_chain_json == ["groq"]

        mock_client.reset_mock()
        cb["data"] = "h3:p:set:1:openai"
        handle_telegram_callback_query(db, mock_client, cb)

        db.refresh(user)
        assert user.ai_provider_chain_json == ["groq", "openai"]

        mock_client.reset_mock()
        cb["data"] = "h3:p:set:2:groq"
        handle_telegram_callback_query(db, mock_client, cb)

        db.refresh(user)
        # Item 22: Duplicate selection rejected cleanly, not silently reordered
        assert user.ai_provider_chain_json == ["groq", "openai"]
        mock_client.answer_callback_query.assert_called_with("cb1", text="Groq is already in your failover chain. Duplicates are not allowed.", show_alert=True)

        mock_client.reset_mock()
        cb["data"] = "h3:p:clear_subs"
        handle_telegram_callback_query(db, mock_client, cb)

        db.refresh(user)
        assert user.ai_provider_chain_json == ["groq"]


def test_ai_models_menu_and_token_selection():
    db = _setup_test_db()
    mock_client = MagicMock()

    cb = {
        "id": "cb_mod",
        "from": {"id": 12345},
        "message": {"message_id": 100, "chat": {"id": 12345, "type": "private"}},
        "data": "h3:settings:models",
    }
    handle_telegram_callback_query(db, mock_client, cb)
    mock_client.edit_message_text.assert_called_once()
    assert "Preferred AI Models" in mock_client.edit_message_text.call_args[1]["text"]

    mock_client.reset_mock()
    cb["data"] = "h3:m:prov:groq"
    handle_telegram_callback_query(db, mock_client, cb)
    assert "Models for Groq" in mock_client.edit_message_text.call_args[1]["text"]

    token = get_model_token("groq", "llama-3.3-70b-versatile")
    mock_client.reset_mock()
    cb["data"] = f"h3:m:set:{token}"
    handle_telegram_callback_query(db, mock_client, cb)

    user = db.query(TelegramUser).filter_by(telegram_user_id=12345).first()
    assert user.ai_models_by_provider_json is not None
    assert user.ai_models_by_provider_json.get("groq") == "llama-3.3-70b-versatile"


def test_speed_and_mode_menus():
    db = _setup_test_db()
    mock_client = MagicMock()

    cb = {
        "id": "cb_sp",
        "from": {"id": 12345},
        "message": {"message_id": 100, "chat": {"id": 12345, "type": "private"}},
        "data": "h3:settings:speed",
    }
    handle_telegram_callback_query(db, mock_client, cb)
    assert "Default Audio Speed" in mock_client.edit_message_text.call_args[1]["text"]

    cb["data"] = "h3:speed:set:1.1"
    handle_telegram_callback_query(db, mock_client, cb)
    user = db.query(TelegramUser).filter_by(telegram_user_id=12345).first()
    assert abs(user.default_speed - 1.1) < 0.01

    cb["data"] = "h3:settings:mode"
    handle_telegram_callback_query(db, mock_client, cb)
    assert "Default Generation Mode" in mock_client.edit_message_text.call_args[1]["text"]

    cb["data"] = "h3:mode:set:research"
    handle_telegram_callback_query(db, mock_client, cb)
    db.refresh(user)
    assert user.default_mode == "research"
