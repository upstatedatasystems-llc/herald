from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.db.models import Base
from herald.telegram.auth import (
    get_or_create_active_pairing_code,
    is_user_authorized,
)
from herald.telegram.bot import handle_telegram_command


def test_telegram_private_chat_enforcement():
    """
    Test that pairing and bot commands are restricted strictly to private chats.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    mock_client = MagicMock()

    with TestingSession() as db:
        code = get_or_create_active_pairing_code(db)

        # 1. Group chat attempt to /pair is rejected
        group_msg = {
            "chat": {"id": -100123456, "type": "supergroup"},
            "from": {"id": 888},
            "message_id": 1,
            "text": f"/pair {code}",
        }
        handle_telegram_command(db, mock_client, group_msg, "pair", code)
        mock_client.send_message.assert_called_with(
            chat_id=-100123456,
            text="⚠️ <b>Herald operates only in private chats.</b>",
            reply_to_message_id=1,
            parse_mode="HTML",
        )

        # 2. Private chat pairing succeeds
        private_msg = {
            "chat": {"id": 888, "type": "private"},
            "from": {"id": 888, "first_name": "Alice"},
            "message_id": 2,
            "text": f"/pair {code}",
        }
        handle_telegram_command(db, mock_client, private_msg, "pair", code)
        assert is_user_authorized(db, user_id=888, chat_id=888) is True
        # User is NOT authorized in an arbitrary group chat
        assert is_user_authorized(db, user_id=888, chat_id=-100999) is False
