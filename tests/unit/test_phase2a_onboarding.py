import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.telegram_bot.main import TELEGRAM_BOT_COMMANDS_2A
from apps.worker.main import send_delivery_nudge, should_send_delivery_nudge
from herald.config import settings
from herald.db.models import Base, PodcastJob, TelegramPairingCode, TelegramUser
from herald.telegram.auth import (
    generate_pairing_code,
    verify_and_claim_pairing_code,
)
from herald.telegram.bot import handle_telegram_command
from herald.telegram.client import TelegramClient
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


def test_pairing_cli_unpaired_truthful_expiry(db_session, monkeypatch):
    """Test pairing CLI returns UNPAIRED:<code>:<remaining_minutes> accurately calculating remaining duration."""
    monkeypatch.setattr("herald.telegram.pairing_cli.SessionLocal", lambda: db_session)

    # 1. No code exists -> creates code with ~30 mins
    status1 = get_pairing_status(expires_in_minutes=30)
    assert status1.startswith("UNPAIRED:")
    parts = status1.split(":")
    assert len(parts) == 3
    code1 = parts[1]
    assert len(code1) == 6
    assert int(parts[2]) in (29, 30)

    # 2. Existing active code with 12 minutes remaining -> reuses same code and reports truthful 12 mins
    pairing_entry = db_session.query(TelegramPairingCode).filter_by(code=code1).first()
    pairing_entry.expires_at = datetime.now(UTC) + timedelta(minutes=12, seconds=10)
    db_session.commit()

    status2 = get_pairing_status(expires_in_minutes=30)
    assert status2 == f"UNPAIRED:{code1}:12"


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
    assert "/settings" in help_text
    assert "/voices" in help_text
    assert "/download" in help_text


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


def test_production_worker_delivery_nudge_logic(monkeypatch):
    """Test actual apps.worker.main delivery nudge helper functions against real conditions."""
    posted_payloads = []

    class MockResponse:
        status_code = 200

    def mock_post(url, *args, **kwargs):
        posted_payloads.append((url, kwargs.get("json")))
        return MockResponse()

    monkeypatch.setattr("httpx.Client.post", mock_post)
    monkeypatch.setattr(settings, "ENABLE_EVENT_DRIVEN_DELIVERY", True)

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

    # 1. Telegram job -> should_send returns False, send_delivery_nudge returns False without POSTing
    assert should_send_delivery_nudge(telegram_job) is False
    res_tg = send_delivery_nudge(telegram_job)
    assert res_tg is False
    assert len(posted_payloads) == 0

    # 2. Email job with ENABLE_EVENT_DRIVEN_DELIVERY=True -> should_send returns True, POST occurs
    assert should_send_delivery_nudge(email_job) is True
    res_email = send_delivery_nudge(email_job)
    assert res_email is True
    assert len(posted_payloads) == 1
    assert posted_payloads[0][1] == {"job_id": "email-job-456", "event": "AUDIO_READY"}

    # 3. Email job with ENABLE_EVENT_DRIVEN_DELIVERY=False -> should_send returns False
    monkeypatch.setattr(settings, "ENABLE_EVENT_DRIVEN_DELIVERY", False)
    assert should_send_delivery_nudge(email_job) is False
    res_disabled = send_delivery_nudge(email_job)
    assert res_disabled is False
    assert len(posted_payloads) == 1  # No new POST

    # 4. Network failure is handled non-fatally
    monkeypatch.setattr(settings, "ENABLE_EVENT_DRIVEN_DELIVERY", True)

    def mock_post_fail(*args, **kwargs):
        raise RuntimeError("Network unreachable")

    monkeypatch.setattr("httpx.Client.post", mock_post_fail)
    res_fail = send_delivery_nudge(email_job)
    assert res_fail is False  # Does not raise RuntimeError


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


def test_compose_yaml_kokoro_service_healthy_dependencies():
    """Test that compose.yaml specifies service_healthy for kokoro across worker and telegram-bot."""
    compose_path = Path("compose.yaml")
    assert compose_path.exists()
    content = compose_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    services = parsed.get("services", {})

    # Kokoro must define healthcheck
    kokoro = services.get("kokoro", {})
    assert "healthcheck" in kokoro
    assert kokoro["healthcheck"]["interval"] == "60s"

    # Worker must depend on kokoro service_healthy
    worker = services.get("herald-worker", {})
    assert worker["depends_on"]["kokoro"]["condition"] == "service_healthy"

    # Telegram bot must depend on kokoro service_healthy
    bot = services.get("telegram-bot", {})
    assert bot["depends_on"]["kokoro"]["condition"] == "service_healthy"


def test_setup_script_zero_secret_leakage_and_settings_presence():
    """Test that setup.sh contains /settings, truthful instructions, and does not leak sentinel secrets."""
    setup_path = Path("setup.sh")
    assert setup_path.exists()
    setup_text = setup_path.read_text(encoding="utf-8")

    # Invariant: No 1-second kokoro curl loop in setup.sh
    assert "docker compose exec -T kokoro curl" not in setup_text

    # Invariant: /settings listed in TELEGRAM COMMANDS
    assert "/settings" in setup_text

    # Verify sentinel secrets are not hardcoded or leaked into template output
    sentinels = [
        "SENTINEL_TELEGRAM_SECRET",
        "SENTINEL_GEMINI_SECRET",
        "SENTINEL_DB_SECRET",
        "SENTINEL_HERALD_SECRET",
    ]
    for s in sentinels:
        assert s not in setup_text
