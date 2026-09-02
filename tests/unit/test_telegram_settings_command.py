from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import Base
from herald.telegram.auth import generate_pairing_code, verify_and_claim_pairing_code
from herald.telegram.bot import handle_telegram_command


def test_telegram_settings_command_runs_against_real_settings():
    """
    Test that /settings executes successfully against real Settings instance
    without raising AttributeError and renders KOKORO_VOICE and KOKORO_SPEED correctly.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    mock_client = MagicMock()
    mock_client.is_configured = True

    with TestingSession() as db:
        code = generate_pairing_code(db)
        verify_and_claim_pairing_code(db, code, user_id=12345, chat_id=12345, username="owner")

        msg = {
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "message_id": 1,
            "text": "/settings",
        }

        handle_telegram_command(db, mock_client, msg, "settings", "")

        mock_client.send_message.assert_called_once()
        call_args = mock_client.send_message.call_args[1]
        text = call_args["text"]

        assert "Instance Settings:" in text
        assert f"Default Voice:</b> <code>{settings.KOKORO_VOICE}</code>" in text
        assert f"Default Speed:</b> <code>{settings.KOKORO_SPEED}x</code>" in text
        assert f"Max Audio Upload:</b> <code>{settings.TELEGRAM_MAX_AUDIO_BYTES / (1024*1024):.0f} MB</code>" in text
