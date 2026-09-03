from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import Base, TelegramUser
from herald.services.voice_manager import (
    ensure_voice_sample,
)
from herald.telegram.auth import generate_pairing_code, verify_and_claim_pairing_code
from herald.telegram.bot import handle_telegram_callback_query, handle_telegram_command
from herald.telegram.client import TelegramClient
from herald.telegram.formatters import format_voices_browser
from herald.tts.kokoro_client import KokoroClient


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


def test_format_voices_browser_lists_all_allowed_voices():
    """format_voices_browser renders all allowed voices with correct inline keyboard buttons."""
    text, markup = format_voices_browser(current_default="af_bella")

    assert "Herald Voice Catalog" in text
    assert "Bella" in text
    assert "Heart" in text
    assert "Adam" in text

    # Verify buttons
    keyboard = markup["inline_keyboard"]
    assert len(keyboard) == len(settings.get_allowed_voices_list())

    # Check Bella row has active marker
    bella_row = [row for row in keyboard if any("af_bella" in btn["callback_data"] for btn in row)][0]
    assert bella_row[0]["callback_data"] == "h2:voice:sample:af_bella"
    assert bella_row[1]["text"] == "✅ Default"


def test_voices_command_sends_interactive_browser(db_session):
    """/voices sends voice browser message with current user default indicated."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)
    msg = {
        "chat": {"id": 12345, "type": "private"},
        "from": {"id": 12345},
        "message_id": 701,
    }

    handle_telegram_command(db_session, mock_client, msg, "voices", "")

    mock_client.send_message.assert_called_once()
    call_args = mock_client.send_message.call_args[1]
    assert "Herald Voice Catalog" in call_args["text"]
    assert "reply_markup" in call_args


def test_voice_set_callback_updates_user_preference_and_edits_message(db_session):
    """h2:voice:set:<voice> updates default voice and refreshes voices browser inline keyboard."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)
    cb_query = {
        "id": "cb-voice-set-1",
        "from": {"id": 12345},
        "message": {"message_id": 702, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:voice:set:am_adam",
    }

    handle_telegram_callback_query(db_session, mock_client, cb_query)

    # Verify preference persisted in DB
    user = db_session.query(TelegramUser).filter_by(telegram_user_id=12345).first()
    assert user is not None
    assert user.default_voice == "am_adam"

    # Verify callback answered and browser message edited
    mock_client.answer_callback_query.assert_called_once()
    assert "am_adam" in mock_client.answer_callback_query.call_args[1]["text"]
    mock_client.edit_message_text.assert_called_once()


def test_voice_set_callback_rejects_invalid_voice(db_session):
    """h2:voice:set:<invalid> shows alert and does not alter database."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)
    cb_query = {
        "id": "cb-voice-set-invalid",
        "from": {"id": 12345},
        "message": {"message_id": 703, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:voice:set:invalid_voice_xyz",
    }

    handle_telegram_callback_query(db_session, mock_client, cb_query)

    mock_client.answer_callback_query.assert_called_once_with(
        "cb-voice-set-invalid", text="Invalid voice 'invalid_voice_xyz'.", show_alert=True
    )
    user = db_session.query(TelegramUser).filter_by(telegram_user_id=12345).first()
    assert user.default_voice != "invalid_voice_xyz"


def test_voice_sample_generation_and_caching(db_session, monkeypatch, tmp_path):
    """ensure_voice_sample synthesizes once, writes persistent cache file, and reuses on subsequent calls."""
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr("herald.services.voice_manager.settings.HERALD_WORK_DIR", str(tmp_path))

    mock_kokoro = MagicMock(spec=KokoroClient)

    def mock_synth(text, output_path, voice=None, speed=None, timeout=None):
        Path(output_path).write_bytes(b"dummy_wav_data")

    mock_kokoro.synthesize_chunk.side_effect = mock_synth

    # 1. First call generates sample
    sample_path = ensure_voice_sample(voice="af_sarah", kokoro_client=mock_kokoro, db=db_session)
    assert sample_path.exists()
    assert sample_path.name == "sample_af_sarah.mp3"
    assert mock_kokoro.synthesize_chunk.call_count == 1

    # 2. Second call reuses cached file on disk without calling KokoroClient
    sample_path2 = ensure_voice_sample(voice="af_sarah", kokoro_client=mock_kokoro, db=db_session)
    assert sample_path2 == sample_path
    assert mock_kokoro.synthesize_chunk.call_count == 1  # Unchanged!


def test_voice_sample_callback_sends_audio_message(db_session, monkeypatch, tmp_path):
    """h2:voice:sample:<voice> triggers sample generation and sends audio with set default button."""
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr("herald.services.voice_manager.settings.HERALD_WORK_DIR", str(tmp_path))

    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    mock_client = MagicMock(spec=TelegramClient)
    cb_query = {
        "id": "cb-voice-sample-1",
        "from": {"id": 12345},
        "message": {"message_id": 704, "chat": {"id": 12345, "type": "private"}},
        "data": "h2:voice:sample:af_bella",
    }

    with patch("herald.telegram.bot.ensure_voice_sample") as mock_ensure:
        dummy_sample = tmp_path / "sample_af_bella.mp3"
        dummy_sample.write_bytes(b"dummy mp3")
        mock_ensure.return_value = dummy_sample

        handle_telegram_callback_query(db_session, mock_client, cb_query)

    mock_client.send_audio.assert_called_once()
    kwargs = mock_client.send_audio.call_args[1]
    assert kwargs["audio_path"] == dummy_sample
    assert "Bella" in kwargs["title"]
    assert "reply_markup" in kwargs
    # Verify inline button to set as default
    button = kwargs["reply_markup"]["inline_keyboard"][0][0]
    assert button["callback_data"] == "h2:voice:set:af_bella"
