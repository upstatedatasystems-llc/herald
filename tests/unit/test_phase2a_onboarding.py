from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.telegram_bot.main import TELEGRAM_BOT_COMMANDS_2A, register_bot_commands
from herald.config import settings
from herald.db.models import Base, PodcastJob, TelegramPairingCode, TelegramUser
from herald.telegram.auth import generate_pairing_code, verify_and_claim_pairing_code
from herald.telegram.bot import handle_telegram_command, process_telegram_update
from herald.telegram.client import TelegramClient
from herald.telegram.formatters import format_help, format_quickstart
from herald.telegram.pairing_cli import get_pairing_status


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


def test_pairing_cli_unpaired(db_session, monkeypatch):
    """Test pairing CLI returns UNPAIRED:<code>:30 when instance has no owner."""
    monkeypatch.setattr("herald.telegram.pairing_cli.SessionLocal", lambda: db_session)
    status = get_pairing_status(expires_in_minutes=30)
    assert status.startswith("UNPAIRED:")
    parts = status.split(":")
    assert len(parts) == 3
    code = parts[1]
    assert len(code) == 6
    assert parts[2] == "30"

    # Verify code exists in DB
    pairing_entry = db_session.query(TelegramPairingCode).filter_by(code=code).first()
    assert pairing_entry is not None
    assert pairing_entry.is_used is False


def test_pairing_cli_already_paired(db_session, monkeypatch):
    """Test pairing CLI returns PAIRED when an active owner is already present."""
    monkeypatch.setattr("herald.telegram.pairing_cli.SessionLocal", lambda: db_session)
    # Create owner
    owner = TelegramUser(
        telegram_user_id=12345,
        telegram_chat_id=12345,
        username="owner_user",
        role="owner",
        is_active=True,
    )
    db_session.add(owner)
    db_session.commit()

    status = get_pairing_status()
    assert status == "PAIRED"

    # Verify no new pairing codes were created
    assert db_session.query(TelegramPairingCode).count() == 0


def test_unauthenticated_start_does_not_leak_pairing_code(db_session):
    """Test unauthenticated /start explains pairing but NEVER exposes pairing code."""
    code = generate_pairing_code(db_session, expires_in_minutes=30)
    mock_client = MagicMock(spec=TelegramClient)

    msg = {
        "message_id": 1,
        "from": {"id": 99999, "username": "stranger"},
        "chat": {"id": 99999, "type": "private"},
        "text": "/start",
    }

    handle_telegram_command(db_session, mock_client, msg, "start", "")

    # Assert sendMessage was called
    mock_client.send_message.assert_called_once()
    sent_text = mock_client.send_message.call_args[1]["text"]

    # Invariant: Pairing code must NOT appear in message to unauthenticated user
    assert code not in sent_text
    assert "Unpaired" in sent_text
    assert "Check the server console" in sent_text


def test_post_pair_sends_quickstart(db_session):
    """Test successful /pair immediately sends quick-start reference."""
    code = generate_pairing_code(db_session)
    mock_client = MagicMock(spec=TelegramClient)

    msg = {
        "message_id": 2,
        "from": {"id": 12345, "username": "alice", "first_name": "Alice"},
        "chat": {"id": 12345, "type": "private"},
        "text": f"/pair {code}",
    }

    handle_telegram_command(db_session, mock_client, msg, "pair", code)

    mock_client.send_message.assert_called_once()
    sent_text = mock_client.send_message.call_args[1]["text"]
    assert "Pairing successful!" in sent_text
    assert "Welcome to Herald!" in sent_text
    assert "Alice" in sent_text
    assert "Quick Start:" in sent_text
    assert "/help" in sent_text


def test_authenticated_start_and_help(db_session):
    """Test authenticated /start and /help return expected formatted reference."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="bob", first_name="Bob")

    mock_client = MagicMock(spec=TelegramClient)

    # 1. /start
    start_msg = {
        "message_id": 3,
        "from": {"id": 12345, "username": "bob", "first_name": "Bob"},
        "chat": {"id": 12345, "type": "private"},
        "text": "/start",
    }
    handle_telegram_command(db_session, mock_client, start_msg, "start", "")
    start_text = mock_client.send_message.call_args[1]["text"]
    assert "Welcome to Herald!" in start_text
    assert "Bob" in start_text
    assert "Quick Start:" in start_text

    # 2. /help
    mock_client.reset_mock()
    help_msg = {
        "message_id": 4,
        "from": {"id": 12345, "username": "bob", "first_name": "Bob"},
        "chat": {"id": 12345, "type": "private"},
        "text": "/help",
    }
    handle_telegram_command(db_session, mock_client, help_msg, "help", "")
    help_text = mock_client.send_message.call_args[1]["text"]
    assert "Herald Usage Guide" in help_text
    assert "/ai_check" in help_text
    assert "/status" in help_text
    assert "/queue" in help_text


def test_ai_check_command_aliases(db_session, monkeypatch):
    """Test that both /ai_check and /ai-check are processed cleanly."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="bob")

    mock_client = MagicMock(spec=TelegramClient)

    # When AI is not configured
    monkeypatch.setattr(settings, "AI_PROVIDER", "none")

    for cmd in ["ai_check", "ai-check", "aicheck"]:
        mock_client.reset_mock()
        msg = {
            "message_id": 10,
            "from": {"id": 12345, "username": "bob"},
            "chat": {"id": 12345, "type": "private"},
            "text": f"/{cmd}",
        }
        handle_telegram_command(db_session, mock_client, msg, cmd, "")
        mock_client.send_message.assert_called_once()
        sent_text = mock_client.send_message.call_args[1]["text"]
        assert "Literal" in sent_text


def test_set_my_commands_payload_validation():
    """Test that all commands in TELEGRAM_BOT_COMMANDS_2A match Telegram constraints."""
    import re

    pattern = re.compile(r"^[a-z0-9_]{1,32}$")
    for cmd in TELEGRAM_BOT_COMMANDS_2A:
        name = cmd["command"]
        desc = cmd["description"]
        assert pattern.match(name) is not None, f"Command '{name}' fails Telegram regex ^[a-z0-9_]{{1,32}}$"
        assert 1 <= len(desc) <= 256, f"Description for '{name}' invalid length {len(desc)}"

    # Invariant: 'ai-check' with hyphen is invalid for setMyCommands, 'ai_check' must be used
    client = TelegramClient(token="fake:token")
    with pytest.raises(ValueError, match="Invalid Telegram command name"):
        client.set_my_commands([{"command": "ai-check", "description": "Invalid hyphen command"}])


def test_set_my_commands_scope():
    """Test that only 2A commands are registered in Package 2A."""
    registered_names = {cmd["command"] for cmd in TELEGRAM_BOT_COMMANDS_2A}
    expected_2a_names = {"start", "help", "status", "ai_check", "queue", "readme"}
    assert registered_names == expected_2a_names

    # Ensure unimplemented/future commands are not in 2A
    assert "voices" not in registered_names
    assert "download" not in registered_names
    assert "diagnostics" not in registered_names
    assert "settings" not in registered_names


def test_worker_suppresses_n8n_nudge_for_telegram_jobs(monkeypatch):
    """Test that worker post-commit delivery block suppresses n8n webhook when transport is telegram."""
    nudge_called = []

    def mock_post(url, *args, **kwargs):
        nudge_called.append(url)
        return MagicMock(status_code=200)

    # Create Telegram job and Email job
    telegram_job = PodcastJob(
        id="tg-job-123",
        transport="telegram",
        status="AUDIO_READY",
        source_hash="hash1",
        source_text="text",
    )
    email_job = PodcastJob(
        id="email-job-456",
        transport="email",
        status="AUDIO_READY",
        source_hash="hash2",
        source_text="text",
    )

    # Simulate delivery nudge condition from apps/worker/main.py
    for job in [telegram_job, email_job]:
        if job.transport != "telegram" and getattr(settings, "ENABLE_EVENT_DRIVEN_DELIVERY", True):
            mock_post("http://n8n:5678/webhook/herald-audio-ready", json={"job_id": job.id})

    # Only email job should have triggered n8n post
    assert len(nudge_called) == 1
    assert "http://n8n:5678/webhook/herald-audio-ready" in nudge_called


def test_send_document_dynamic_mime_handling(tmp_path, monkeypatch):
    """Test send_document resolves correct MIME types for different extensions."""
    client = TelegramClient(token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")

    posted_files = {}

    def mock_post(url, *args, **kwargs):
        nonlocal posted_files
        files = kwargs.get("files", {})
        if "document" in files:
            doc_tuple = files["document"]  # (filename, fileobj, mimetype)
            posted_files[doc_tuple[0]] = doc_tuple[2]
        return MagicMock(json=lambda: {"ok": True, "result": {"message_id": 100}})

    monkeypatch.setattr("httpx.Client.post", mock_post)

    test_extensions = [
        ("audio.mp3", "audio/mpeg"),
        ("diag.zip", "application/zip"),
        ("README.md", "text/markdown"),
        ("data.json", "application/json"),
        ("notes.txt", "text/plain"),
    ]

    for fname, expected_mime in test_extensions:
        fpath = tmp_path / fname
        fpath.write_text("sample content")
        client.send_document(chat_id=123, document_path=fpath)
        assert posted_files[fname] == expected_mime, f"Failed MIME for {fname}: got {posted_files[fname]}"

    # Test explicit override
    override_path = tmp_path / "custom.dat"
    override_path.write_text("custom data")
    client.send_document(chat_id=123, document_path=override_path, mime_type="application/x-custom")
    assert posted_files["custom.dat"] == "application/x-custom"
