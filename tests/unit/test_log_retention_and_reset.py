"""
Unit tests for log retention across resets/reinstalls and pairing CLI read-only inspection.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlalchemy.orm import Session

from herald.db.models import TelegramPairingCode, TelegramUser
from herald.telegram.pairing_cli import get_pairing_status


def test_pairing_cli_read_only_when_no_code_exists(db_session: Session, monkeypatch):
    """Verify pairing_cli with read_only=True returns NONE when no pairing code exists."""
    monkeypatch.setattr("herald.telegram.pairing_cli.SessionLocal", lambda: db_session)

    status = get_pairing_status(read_only=True)
    assert status == "NONE"

    # Verify no record was created in database
    count = db_session.query(TelegramPairingCode).count()
    assert count == 0


def test_pairing_cli_read_only_returns_existing_code(db_session: Session, monkeypatch):
    """Verify pairing_cli with read_only=True returns existing active code without generating a new one."""
    monkeypatch.setattr("herald.telegram.pairing_cli.SessionLocal", lambda: db_session)

    # Insert an existing unexpired pairing code
    now = datetime.now(UTC)
    record = TelegramPairingCode(
        code="987654",
        is_used=False,
        expires_at=now + timedelta(minutes=25),
        created_at=now,
    )
    db_session.add(record)
    db_session.commit()

    status = get_pairing_status(read_only=True)
    assert status.startswith("UNPAIRED:987654:")
    remaining_mins = int(status.split(":")[2])
    assert 24 <= remaining_mins <= 26

    # Verify still exactly 1 record in database (no new code generated)
    count = db_session.query(TelegramPairingCode).count()
    assert count == 1


def test_pairing_cli_read_only_when_already_paired(db_session: Session, monkeypatch):
    """Verify pairing_cli with read_only=True returns PAIRED if an owner exists."""
    monkeypatch.setattr("herald.telegram.pairing_cli.SessionLocal", lambda: db_session)

    owner = TelegramUser(
        telegram_user_id=123456789,
        telegram_chat_id=123456789,
        username="herald_owner",
        role="owner",
    )
    db_session.add(owner)
    db_session.commit()

    status = get_pairing_status(read_only=True)
    assert status == "PAIRED"


def test_install_script_preserves_logs_directory(tmp_path: Path):
    """Verify git clean -fd -e logs -e logs/* preserves logs/ and existing files."""
    import subprocess
    import shutil

    git_bin = shutil.which("git")
    if not git_bin:
        return

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    # Initialize a dummy git repo
    subprocess.run([git_bin, "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run([git_bin, "config", "user.email", "test@test.com"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run([git_bin, "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)

    dummy_file = repo_dir / "README.md"
    dummy_file.write_text("# Hello", encoding="utf-8")
    subprocess.run([git_bin, "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run([git_bin, "commit", "-m", "initial"], cwd=repo_dir, check=True, capture_output=True)

    # Create logs directory with log files
    logs_dir = repo_dir / "logs"
    logs_dir.mkdir()
    install_log = logs_dir / "install-20260909-120000.log"
    install_log.write_text("install log transcript", encoding="utf-8")

    bot_log = logs_dir / "telegram-bot.log"
    bot_log.write_text("bot log content", encoding="utf-8")

    diag_dir = logs_dir / "diagnostics"
    diag_dir.mkdir()
    diag_file = diag_dir / "job1_COMPLETE.zip"
    diag_file.write_bytes(b"PK dummy")

    # Run git clean -fd -e logs -e logs/*
    res = subprocess.run([git_bin, "clean", "-fd", "-e", "logs", "-e", "logs/*"], cwd=repo_dir, capture_output=True)
    assert res.returncode == 0

    # Verify logs directory and contents survived completely
    assert logs_dir.exists()
    assert install_log.exists()
    assert install_log.read_text(encoding="utf-8") == "install log transcript"
    assert bot_log.exists()
    assert diag_file.exists()
