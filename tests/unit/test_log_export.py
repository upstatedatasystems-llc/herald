"""
Unit tests for the owner-only Telegram /logs export command.

Covers:
- Command argument parsing and timezone handling
- Owner-only authorization enforcement
- Service log filtering with multiline and rotated file support
- Canonical diagnostics archive selection and preservation
- ZIP bundle creation, relative paths, and Telegram delivery
- Temporary file cleanup under success and failure conditions
- /help and bot command registration
"""

import os
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apps.telegram_bot.main import TELEGRAM_BOT_COMMANDS
from herald.config import settings
from herald.db.models import TelegramUser
from herald.services.log_export import (
    LogsArgumentError,
    build_log_export_archive,
    collect_matching_diagnostics,
    collect_matching_service_logs,
    filter_log_file_content,
    generate_export_zip_name,
    get_configured_timezone,
    parse_logs_command_args,
)
from herald.telegram.auth import generate_pairing_code, verify_and_claim_pairing_code
from herald.telegram.bot import handle_telegram_command
from herald.telegram.client import TelegramClient
from herald.telegram.formatters import format_help

# ==============================================================================
# 1. COMMAND PARSING & TIMEZONE BEHAVIOR
# ==============================================================================

def test_parse_args_date_only():
    """1. /logs YYYY-MM-DD resolves to 00:00 in settings.TZ."""
    tz = get_configured_timezone(settings.TZ)
    fixed_now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=tz)

    start_dt, end_dt = parse_logs_command_args("2026-09-13", tz=tz, now=fixed_now)

    assert start_dt == datetime(2026, 9, 13, 0, 0, 0, tzinfo=tz)
    assert end_dt == fixed_now
    assert start_dt.tzinfo == tz


def test_parse_args_date_and_time():
    """2. /logs YYYY-MM-DD HH:MM resolves to specified hour:minute in settings.TZ."""
    tz = get_configured_timezone(settings.TZ)
    fixed_now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=tz)

    start_dt, end_dt = parse_logs_command_args("2026-09-13 18:30", tz=tz, now=fixed_now)

    assert start_dt == datetime(2026, 9, 13, 18, 30, 0, tzinfo=tz)
    assert end_dt == fixed_now


def test_parse_args_missing_argument():
    """Missing or whitespace arguments reject with usage instructions."""
    tz = get_configured_timezone(settings.TZ)
    with pytest.raises(LogsArgumentError, match="Usage:"):
        parse_logs_command_args("", tz=tz)

    with pytest.raises(LogsArgumentError, match="Usage:"):
        parse_logs_command_args("   ", tz=tz)


def test_parse_args_malformed_date():
    """3. Malformed date syntax is rejected."""
    tz = get_configured_timezone(settings.TZ)
    for bad in ["2026/09/13", "09-13-2026", "yesterday", "2026-9-13"]:
        with pytest.raises(LogsArgumentError):
            parse_logs_command_args(bad, tz=tz)


def test_parse_args_malformed_time():
    """4. Malformed clock time syntax is rejected."""
    tz = get_configured_timezone(settings.TZ)
    for bad in ["2026-09-13 18", "2026-09-13 18:3", "2026-09-13 6pm", "2026-09-13 18:30:00"]:
        with pytest.raises(LogsArgumentError):
            parse_logs_command_args(bad, tz=tz)


def test_parse_args_impossible_calendar_date():
    """5. Impossible calendar dates (e.g. Feb 31, month 13) are rejected."""
    tz = get_configured_timezone(settings.TZ)
    for bad in ["2026-02-31", "2026-13-01", "2026-04-31"]:
        with pytest.raises(LogsArgumentError, match="Invalid calendar date"):
            parse_logs_command_args(bad, tz=tz)


def test_parse_args_impossible_clock_time():
    """6. Impossible clock times (e.g. 25:00, 18:99) are rejected."""
    tz = get_configured_timezone(settings.TZ)
    for bad in ["2026-09-13 25:00", "2026-09-13 18:99", "2026-09-13 24:00"]:
        with pytest.raises(LogsArgumentError, match="Invalid clock time"):
            parse_logs_command_args(bad, tz=tz)


def test_parse_args_extra_arguments():
    """7. Extra arguments after the date/time are rejected."""
    tz = get_configured_timezone(settings.TZ)
    with pytest.raises(LogsArgumentError, match="Invalid date/time syntax"):
        parse_logs_command_args("2026-09-13 18:30 extra_token", tz=tz)

    with pytest.raises(LogsArgumentError, match="Invalid date/time syntax"):
        parse_logs_command_args("2026-09-13 extra", tz=tz)


def test_parse_args_future_cutoff_rejected():
    """8. Start time in the future is rejected."""
    tz = get_configured_timezone(settings.TZ)
    fixed_now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=tz)

    with pytest.raises(LogsArgumentError, match="future"):
        parse_logs_command_args("2026-09-14 12:01", tz=tz, now=fixed_now)

    with pytest.raises(LogsArgumentError, match="future"):
        parse_logs_command_args("2026-09-15", tz=tz, now=fixed_now)


# ==============================================================================
# 2. OWNER-ONLY AUTHORIZATION
# ==============================================================================

def test_owner_can_run_logs_command(db_session, monkeypatch, tmp_path):
    """9. Paired owner can successfully execute /logs."""
    monkeypatch.setattr(settings, "HERALD_LOG_DIR", str(tmp_path))
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=1001, chat_id=1001, username="owner_user")

    mock_client = MagicMock(spec=TelegramClient)
    msg = {
        "message_id": 10,
        "from": {"id": 1001, "username": "owner_user"},
        "chat": {"id": 1001, "type": "private"},
        "text": "/logs 2026-09-13",
    }

    handle_telegram_command(db_session, mock_client, msg, "logs", "2026-09-13")

    # With no logs in tmp_path, owner receives the "no logs found" notice (not access denied)
    mock_client.send_message.assert_called_once()
    sent_text = mock_client.send_message.call_args[1]["text"]
    assert "No Herald logs or diagnostics were found" in sent_text


def test_generic_authorized_non_owner_rejected(db_session, monkeypatch, tmp_path):
    """10. Generic authorized user who is NOT paired owner cannot run /logs."""
    monkeypatch.setattr(settings, "HERALD_LOG_DIR", str(tmp_path))

    # Pair legitimate owner
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=1001, chat_id=1001, username="owner_user")

    # Create active regular user in DB
    user2 = TelegramUser(
        telegram_user_id=2002,
        telegram_chat_id=2002,
        username="regular_user",
        role="user",
        is_active=True,
    )
    db_session.add(user2)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    msg = {
        "message_id": 11,
        "from": {"id": 2002, "username": "regular_user"},
        "chat": {"id": 2002, "type": "private"},
        "text": "/logs 2026-09-13",
    }

    handle_telegram_command(db_session, mock_client, msg, "logs", "2026-09-13")

    mock_client.send_message.assert_called_once()
    sent_text = mock_client.send_message.call_args[1]["text"]
    assert "Access Denied" in sent_text
    assert "restricted to the paired owner" in sent_text


def test_allowed_user_ids_setting_cannot_bypass_owner_check(db_session, monkeypatch, tmp_path):
    """10b. User in TELEGRAM_ALLOWED_USER_IDS cannot run /logs unless active paired owner."""
    monkeypatch.setattr(settings, "HERALD_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", "3003,4004")

    # Legitimate owner is 1001
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=1001, chat_id=1001)

    mock_client = MagicMock(spec=TelegramClient)
    msg = {
        "message_id": 12,
        "from": {"id": 3003, "username": "allowed_guest"},
        "chat": {"id": 3003, "type": "private"},
        "text": "/logs 2026-09-13",
    }

    handle_telegram_command(db_session, mock_client, msg, "logs", "2026-09-13")

    mock_client.send_message.assert_called_once()
    sent_text = mock_client.send_message.call_args[1]["text"]
    assert "Access Denied" in sent_text


def test_owner_wrong_chat_id_rejected(db_session, monkeypatch, tmp_path):
    """Owner user sending command from an unverified chat is rejected."""
    monkeypatch.setattr(settings, "HERALD_LOG_DIR", str(tmp_path))
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=1001, chat_id=1001)

    mock_client = MagicMock(spec=TelegramClient)
    msg = {
        "message_id": 13,
        "from": {"id": 1001},
        "chat": {"id": 9999, "type": "private"},  # Mismatched chat ID
        "text": "/logs 2026-09-13",
    }

    handle_telegram_command(db_session, mock_client, msg, "logs", "2026-09-13")

    sent_text = mock_client.send_message.call_args[1]["text"]
    assert "Access Denied" in sent_text


def test_denied_caller_no_filesystem_touch(db_session, monkeypatch, tmp_path):
    """11. Denied caller causes zero filesystem or export service activity."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=1001, chat_id=1001)

    mock_client = MagicMock(spec=TelegramClient)
    msg = {
        "message_id": 14,
        "from": {"id": 9999},
        "chat": {"id": 9999, "type": "private"},
        "text": "/logs 2026-09-13",
    }

    with patch("herald.telegram.bot.collect_matching_service_logs") as mock_logs, \
         patch("herald.telegram.bot.collect_matching_diagnostics") as mock_diags, \
         patch("herald.telegram.bot.tempfile.mkdtemp") as mock_temp:
        handle_telegram_command(db_session, mock_client, msg, "logs", "2026-09-13")

        mock_logs.assert_not_called()
        mock_diags.assert_not_called()
        mock_temp.assert_not_called()


# ==============================================================================
# 3. SERVICE LOG FILTERING & MULTILINE RECORDS
# ==============================================================================

def test_service_log_filtering_boundaries_and_multiline():
    """
    12-17. Verifies:
    - Log records before cutoff excluded (12)
    - Log records exactly at cutoff included (13)
    - Log records after cutoff included (14)
    - Records after export_end excluded (15)
    - Multiline traceback lines stay attached to included parent record (16)
    - Multiline records before cutoff are entirely excluded (17)
    """
    tz = get_configured_timezone("America/New_York")
    start = datetime(2026, 9, 13, 18, 30, 0, tzinfo=tz)
    end = datetime(2026, 9, 14, 8, 0, 0, tzinfo=tz)

    sample_log = """2026-09-13 18:29:59,999 [INFO] [worker] Record before cutoff
Traceback (most recent call last):
  File "test.py", line 1, in <module>
RuntimeError: should be excluded entirely
2026-09-13 18:30:00,000 [INFO] [worker] Record exactly at cutoff
2026-09-13 20:15:00,123 [ERROR] [daemon] Job failed
Traceback (most recent call last):
  File "runner.py", line 42, in run
    raise ValueError("Something broke")
ValueError: Something broke
2026-09-14 07:59:59,500 [INFO] [worker] Record within window
2026-09-14 08:00:00,000 [INFO] [worker] Record exactly at end boundary
2026-09-14 08:00:01,000 [INFO] [worker] Record after end boundary
Another continuation line from excluded record
"""

    filtered, count = filter_log_file_content(sample_log, start, end, tz)

    assert count == 4
    # Check that excluded record and its traceback are gone
    assert "Record before cutoff" not in filtered
    assert "should be excluded entirely" not in filtered

    # Check included records
    assert "Record exactly at cutoff" in filtered
    assert "Job failed" in filtered
    assert 'raise ValueError("Something broke")' in filtered
    assert "Record within window" in filtered
    assert "Record exactly at end boundary" in filtered

    # Check that records after end boundary are gone
    assert "Record after end boundary" not in filtered
    assert "Another continuation line" not in filtered


def test_rotated_log_files_included(tmp_path):
    """18. Rotated .log.1 and original .log files are both collected."""
    tz = get_configured_timezone("America/New_York")
    start = datetime(2026, 9, 13, 0, 0, 0, tzinfo=tz)
    end = datetime(2026, 9, 14, 12, 0, 0, tzinfo=tz)

    # Active log file
    (tmp_path / "herald-worker.log").write_text(
        "2026-09-14 08:00:00,000 [INFO] [worker] Active worker log\n",
        encoding="utf-8",
    )
    # Rotated log file
    (tmp_path / "herald-worker.log.1").write_text(
        "2026-09-13 12:00:00,000 [INFO] [worker] Historical rotated log\n",
        encoding="utf-8",
    )
    # Different service log
    (tmp_path / "telegram-bot.log").write_text(
        "2026-09-13 15:00:00,000 [INFO] [bot] Bot service log\n",
        encoding="utf-8",
    )

    matched, count = collect_matching_service_logs(tmp_path, start, end, tz)

    assert count == 3
    assert "logs/herald-worker.log" in matched
    assert "logs/herald-worker.log.1" in matched
    assert "logs/telegram-bot.log" in matched
    assert "Active worker log" in matched["logs/herald-worker.log"]
    assert "Historical rotated log" in matched["logs/herald-worker.log.1"]


def test_malformed_and_non_timestamped_data_safe():
    """19. Malformed/non-timestamped input does not crash the export."""
    tz = get_configured_timezone("America/New_York")
    start = datetime(2026, 9, 13, 0, 0, 0, tzinfo=tz)
    end = datetime(2026, 9, 14, 12, 0, 0, tzinfo=tz)

    junk = """[BOGUS HEADER WITHOUT TIMESTAMP]
Random unformatted line
2026-09-13 10:00:00,000 [INFO] [worker] Legitimate record
Trailing line
"""
    filtered, count = filter_log_file_content(junk, start, end, tz)

    assert count == 1
    assert "BOGUS HEADER" not in filtered
    assert "Random unformatted line" not in filtered
    assert "Legitimate record" in filtered
    assert "Trailing line" in filtered


# ==============================================================================
# 4. DIAGNOSTICS ARCHIVE SELECTION
# ==============================================================================

def test_diagnostics_selection_by_mtime(tmp_path):
    """
    20-24. Canonical diagnostics ZIP selection:
    - Diagnostic ZIP in range included (20)
    - Diagnostic ZIP before range excluded (21)
    - Diagnostic ZIP after end excluded (22)
    - Temp/incomplete files (.tmp.*) excluded (23)
    - Canonical ZIP remains intact and not modified (24)
    """
    diag_dir = tmp_path / "diagnostics"
    diag_dir.mkdir()

    tz = get_configured_timezone("America/New_York")
    start = datetime(2026, 9, 13, 10, 0, 0, tzinfo=tz)
    end = datetime(2026, 9, 13, 20, 0, 0, tzinfo=tz)

    # 1. Canonical ZIP within window (14:00)
    zip_in = diag_dir / "job1_COMPLETE.zip"
    with zipfile.ZipFile(zip_in, "w") as zf:
        zf.writestr("manifest.json", '{"job_id": "job1"}')
    t_in = datetime(2026, 9, 13, 14, 0, 0, tzinfo=tz).timestamp()
    os.utime(zip_in, (t_in, t_in))

    # 2. Canonical ZIP before window (08:00)
    zip_before = diag_dir / "job0_COMPLETE.zip"
    with zipfile.ZipFile(zip_before, "w") as zf:
        zf.writestr("manifest.json", '{"job_id": "job0"}')
    t_before = datetime(2026, 9, 13, 8, 0, 0, tzinfo=tz).timestamp()
    os.utime(zip_before, (t_before, t_before))

    # 3. Canonical ZIP after window (22:00)
    zip_after = diag_dir / "job2_COMPLETE.zip"
    with zipfile.ZipFile(zip_after, "w") as zf:
        zf.writestr("manifest.json", '{"job_id": "job2"}')
    t_after = datetime(2026, 9, 13, 22, 0, 0, tzinfo=tz).timestamp()
    os.utime(zip_after, (t_after, t_after))

    # 4. Staging .tmp file within window
    tmp_file = diag_dir / "job3.tmp.12345.zip"
    tmp_file.write_text("staging data")
    os.utime(tmp_file, (t_in, t_in))

    # 5. Non-zip file
    txt_file = diag_dir / "notes.txt"
    txt_file.write_text("diagnostic note")
    os.utime(txt_file, (t_in, t_in))

    matched = collect_matching_diagnostics(diag_dir, start, end, tz)

    assert len(matched) == 1
    assert matched[0].name == "job1_COMPLETE.zip"

    # Canonical source files must still exist and be intact
    assert zip_in.exists()
    assert zip_before.exists()
    assert zip_after.exists()


# ==============================================================================
# 5. ARCHIVE CREATION, DELIVERY, & CLEANUP
# ==============================================================================

def test_outer_zip_filename_and_layout(tmp_path):
    """25-26. Outer ZIP filename contains timestamps and relative layout is preserved."""
    tz = get_configured_timezone("America/New_York")
    start = datetime(2026, 9, 13, 18, 30, 0, tzinfo=tz)
    end = datetime(2026, 9, 14, 8, 5, 0, tzinfo=tz)

    fname = generate_export_zip_name(start, end)
    assert fname == "herald-logs-20260913-1830-to-20260914-0805.zip"

    # Create dummy diagnostic zip to bundle
    diag_zip = tmp_path / "jobX_COMPLETE.zip"
    with zipfile.ZipFile(diag_zip, "w") as zf:
        zf.writestr("evidence.json", "{}")

    output_zip = tmp_path / fname
    filtered_logs = {
        "logs/herald-worker.log": "Worker log entry",
        "logs/telegram-bot.log": "Bot log entry",
    }

    build_log_export_archive(output_zip, filtered_logs, [diag_zip])

    assert output_zip.exists()
    with zipfile.ZipFile(output_zip, "r") as zf:
        names = zf.namelist()
        assert "logs/herald-worker.log" in names
        assert "logs/telegram-bot.log" in names
        assert "logs/diagnostics/jobX_COMPLETE.zip" in names

        # Verify nested ZIP is untouched
        nested_bytes = zf.read("logs/diagnostics/jobX_COMPLETE.zip")
        assert len(nested_bytes) > 0


def test_no_matching_data_sends_no_document(db_session, monkeypatch, tmp_path):
    """27. When no logs/diagnostics match, no ZIP is generated or uploaded."""
    monkeypatch.setattr(settings, "HERALD_LOG_DIR", str(tmp_path))
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=1001, chat_id=1001)

    mock_client = MagicMock(spec=TelegramClient)
    msg = {
        "message_id": 20,
        "from": {"id": 1001},
        "chat": {"id": 1001, "type": "private"},
        "text": "/logs 2026-09-13",
    }

    handle_telegram_command(db_session, mock_client, msg, "logs", "2026-09-13")

    mock_client.send_document.assert_not_called()
    mock_client.send_message.assert_called_once()
    assert "No Herald logs or diagnostics were found" in mock_client.send_message.call_args[1]["text"]


def test_successful_export_delivery_and_temp_cleanup(db_session, monkeypatch, tmp_path):
    """28-29, 31. On success, document is delivered, temp ZIP is removed, source logs untouched."""
    monkeypatch.setattr(settings, "HERALD_LOG_DIR", str(tmp_path))
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=1001, chat_id=1001)

    # Create matching log file in HERALD_LOG_DIR
    log_file = tmp_path / "herald-worker.log"
    log_file.write_text(
        "2026-09-13 12:00:00,000 [INFO] [worker] Matching work item\n",
        encoding="utf-8",
    )

    mock_client = MagicMock(spec=TelegramClient)
    msg = {
        "message_id": 21,
        "from": {"id": 1001},
        "chat": {"id": 1001, "type": "private"},
        "text": "/logs 2026-09-13",
    }

    uploaded_doc_path = None

    def capture_send_document(*args, **kwargs):
        nonlocal uploaded_doc_path
        uploaded_doc_path = Path(kwargs["document_path"])
        # File must exist during send_document call
        assert uploaded_doc_path.exists()
        return {"ok": True}

    mock_client.send_document.side_effect = capture_send_document

    handle_telegram_command(db_session, mock_client, msg, "logs", "2026-09-13")

    mock_client.send_document.assert_called_once()
    caption = mock_client.send_document.call_args[1]["caption"]
    assert "Herald logs" in caption
    assert "From: 2026-09-13 00:00" in caption
    assert settings.TZ in caption

    # After command execution finishes, temp ZIP file must be cleaned up
    assert uploaded_doc_path is not None
    assert not uploaded_doc_path.exists()

    # Canonical source log file remains untouched
    assert log_file.exists()
    assert "Matching work item" in log_file.read_text(encoding="utf-8")


def test_temp_cleanup_when_send_document_raises(db_session, monkeypatch, tmp_path):
    """30. Temporary outer ZIP is removed even when send_document raises an exception."""
    monkeypatch.setattr(settings, "HERALD_LOG_DIR", str(tmp_path))
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=1001, chat_id=1001)

    log_file = tmp_path / "herald-worker.log"
    log_file.write_text(
        "2026-09-13 12:00:00,000 [INFO] [worker] Matching record\n",
        encoding="utf-8",
    )

    mock_client = MagicMock(spec=TelegramClient)
    msg = {
        "message_id": 22,
        "from": {"id": 1001},
        "chat": {"id": 1001, "type": "private"},
        "text": "/logs 2026-09-13",
    }

    staged_path = None

    def failing_send_document(*args, **kwargs):
        nonlocal staged_path
        staged_path = Path(kwargs["document_path"])
        assert staged_path.exists()
        raise RuntimeError("Telegram API timeout")

    mock_client.send_document.side_effect = failing_send_document

    handle_telegram_command(db_session, mock_client, msg, "logs", "2026-09-13")

    # Error message sent to user
    mock_client.send_message.assert_called_once()
    assert "Export Failed" in mock_client.send_message.call_args[1]["text"]

    # Temp file must no longer exist
    assert staged_path is not None
    assert not staged_path.exists()


# ==============================================================================
# 6. HELP & COMMAND REGISTRATION
# ==============================================================================

def test_format_help_includes_logs_command():
    """32. format_help() includes the /logs command reference."""
    help_text = format_help()
    assert "/logs YYYY-MM-DD [HH:MM]" in help_text
    assert "Owner-only" in help_text


def test_telegram_bot_commands_includes_logs():
    """33. TELEGRAM_BOT_COMMANDS includes logs."""
    cmd_names = [c["command"] for c in TELEGRAM_BOT_COMMANDS]
    assert "logs" in cmd_names

    logs_entry = next(c for c in TELEGRAM_BOT_COMMANDS if c["command"] == "logs")
    assert "Export logs and diagnostics" in logs_entry["description"]
