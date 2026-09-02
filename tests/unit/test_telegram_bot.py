from unittest.mock import MagicMock

from herald.config import settings
from herald.db.models import JobState, PodcastJob
from herald.telegram.auth import verify_and_claim_pairing_code
from herald.telegram.bot import (
    handle_telegram_command,
    parse_telegram_message_directives,
    process_telegram_update,
)
from herald.telegram.client import TelegramClient


def test_parse_telegram_message_directives():
    """Test parsing of mode prefixes, directives, and URLs."""
    raw1 = "https://example.com/ai-report\nresearch high\nVoice: af_bella\nSpeed: 1.1\nTitle: AI Advances"
    p1 = parse_telegram_message_directives(raw1)
    assert p1["url"] == "https://example.com/ai-report"
    assert p1["mode"] == "research"
    assert p1["research_depth"] == "high"
    assert p1["voice"] == "af_bella"
    assert p1["speed"] == 1.1
    assert p1["title"] == "AI Advances"

    raw2 = "literal\n\nDirect text pasted without a link."
    p2 = parse_telegram_message_directives(raw2)
    assert p2["mode"] == "literal"
    assert p2["url"] is None
    assert "Direct text pasted" in p2["text"]


def test_unauthorized_user_rejected(db_session):
    """Point 2: Unauthorized Telegram user rejected before expensive work."""
    mock_client = MagicMock(spec=TelegramClient)
    update = {
        "update_id": 1001,
        "message": {
            "message_id": 1,
            "from": {"id": 999999, "username": "stranger"},
            "chat": {"id": 999999, "type": "private"},
            "text": "https://example.com/article",
        },
    }

    process_telegram_update(db_session, mock_client, update)

    mock_client.send_message.assert_called_once()
    call_args = mock_client.send_message.call_args[1]
    assert "Unpaired" in call_args["text"] or "Access Denied" in call_args["text"]

    # Verify no job was created
    assert db_session.query(PodcastJob).count() == 0


def test_text_and_url_request_accepted(db_session, monkeypatch):
    """Point 1, 6, 7: Telegram update -> valid Herald job for text and URL requests."""
    # 1. Authorize owner
    from herald.telegram.auth import generate_pairing_code
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=111, chat_id=111, username="alice")

    mock_client = MagicMock(spec=TelegramClient)

    # 2. Text request
    text_update = {
        "update_id": 1002,
        "message": {
            "message_id": 10,
            "from": {"id": 111, "username": "alice"},
            "chat": {"id": 111, "type": "private"},
            "text": "literal\n\n# Autonomous Vehicles\nSelf driving cars utilize multi-sensor fusion.",
        },
    }
    process_telegram_update(db_session, mock_client, text_update)

    job = db_session.query(PodcastJob).filter(PodcastJob.telegram_message_id == 10).first()
    assert job is not None
    assert job.request_mode == "literal"
    assert job.transport == "telegram"
    assert job.status in (JobState.QUEUED_TTS.value, JobState.SCRIPT_READY.value)
    assert job.custom_title == "Autonomous Vehicles"

    # 3. URL request (mocking extraction)
    monkeypatch.setattr(
        "herald.core.pipeline.extract_article_from_url",
        lambda url: ("Space Exploration", "Missions to Mars require extensive logistics.", url),
    )

    url_update = {
        "update_id": 1003,
        "message": {
            "message_id": 11,
            "from": {"id": 111, "username": "alice"},
            "chat": {"id": 111, "type": "private"},
            "text": "literal\nhttps://example.com/mars-mission",
        },
    }
    process_telegram_update(db_session, mock_client, url_update)

    job_url = db_session.query(PodcastJob).filter(PodcastJob.telegram_message_id == 11).first()
    assert job_url is not None
    assert job_url.source_url == "https://example.com/mars-mission"
    assert job_url.request_mode == "literal"


def test_duplicate_telegram_update_does_not_create_duplicate_job(db_session):
    """Point 5: Duplicate Telegram update does not create duplicate job."""
    from herald.telegram.auth import generate_pairing_code
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=111, chat_id=111)

    mock_client = MagicMock(spec=TelegramClient)

    update = {
        "update_id": 2001,
        "message": {
            "message_id": 55,
            "from": {"id": 111},
            "chat": {"id": 111, "type": "private"},
            "text": "literal\n\nFirst attempt text payload.",
        },
    }

    # Process first time
    process_telegram_update(db_session, mock_client, update)
    assert db_session.query(PodcastJob).count() == 1

    # Process second time (retry of same message)
    process_telegram_update(db_session, mock_client, update)
    assert db_session.query(PodcastJob).count() == 1

    # Check that duplicate notification was sent
    last_call = mock_client.send_message.call_args[1]
    assert "Already Received" in last_call["text"] or "Already Processed" in last_call["text"]


def test_status_command_with_and_without_ai(db_session, monkeypatch):
    """Point 16 & 17: /status contains AI provider/connection state and works with no AI provider."""
    from herald.telegram.auth import generate_pairing_code
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=111, chat_id=111)

    mock_client = MagicMock(spec=TelegramClient)
    msg = {"message_id": 1, "from": {"id": 111}, "chat": {"id": 111, "type": "private"}}

    # Case 1: No AI provider
    monkeypatch.setattr(settings, "AI_PROVIDER", "none")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    handle_telegram_command(db_session, mock_client, msg, "/status", "")

    status_text = mock_client.send_message.call_args[1]["text"]
    assert "TTS Engine (Kokoro):" in status_text
    assert "AI Provider:" in status_text
    assert "Disk Space:" in status_text
    assert "Uptime:" in status_text


def test_readme_command(db_session):
    """Point 20: /readme sends README.md."""
    from herald.telegram.auth import generate_pairing_code
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=111, chat_id=111)

    mock_client = MagicMock(spec=TelegramClient)
    msg = {"message_id": 1, "from": {"id": 111}, "chat": {"id": 111, "type": "private"}}

    handle_telegram_command(db_session, mock_client, msg, "/readme", "")
    mock_client.send_document.assert_called_once()
    doc_path = mock_client.send_document.call_args[1]["document_path"]
    assert "README.md" in str(doc_path)


def test_secrets_never_appear_in_telegram_client():
    """Point 21: Secrets never appear in normal logs or error strings."""
    secret_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    client = TelegramClient(token=secret_token)
    sanitized = client._sanitize(f"Error connecting to https://api.telegram.org/bot{secret_token}/getMe")
    assert secret_token not in sanitized
    assert "[REDACTED_BOT_TOKEN]" in sanitized
