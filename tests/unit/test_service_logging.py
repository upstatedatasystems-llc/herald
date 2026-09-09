"""
Unit tests for Herald persistent rotating service logging.
Verifies handler configuration, idempotency, rotation limits, secret scrubbing, and permission fallback.
"""

import logging
from pathlib import Path
from unittest.mock import patch

from herald.logging import (
    RedactingFormatter,
    clear_registered_secrets,
    register_secret,
    setup_service_logging,
)


def test_setup_service_logging_initialization_and_idempotency(tmp_path: Path):
    log_dir = tmp_path / "logs"
    service_name = "test-service"

    # First setup
    setup_service_logging(service_name=service_name, log_dir=str(log_dir))

    root = logging.getLogger()
    stdout_handlers = [h for h in root.handlers if getattr(h, "_herald_handler_id", None) == "herald_stdout"]
    file_handlers = [
        h for h in root.handlers if getattr(h, "_herald_handler_id", None) == f"herald_file_{service_name}"
    ]

    assert len(stdout_handlers) == 1
    assert len(file_handlers) == 1

    # Second setup (verify idempotency, handlers scrubbed and not duplicated)
    setup_service_logging(service_name=service_name, log_dir=str(log_dir))

    stdout_handlers_2 = [h for h in root.handlers if getattr(h, "_herald_handler_id", None) == "herald_stdout"]
    file_handlers_2 = [
        h for h in root.handlers if getattr(h, "_herald_handler_id", None) == f"herald_file_{service_name}"
    ]

    assert len(stdout_handlers_2) == 1
    assert len(file_handlers_2) == 1


def test_rotating_file_handler_properties(tmp_path: Path):
    log_dir = tmp_path / "logs"
    service_name = "worker-test"
    max_bytes = 1024 * 1024
    backup_count = 2

    setup_service_logging(
        service_name=service_name,
        log_dir=str(log_dir),
        max_bytes=max_bytes,
        backup_count=backup_count,
    )

    root = logging.getLogger()
    file_handler = next(
        h for h in root.handlers if getattr(h, "_herald_handler_id", None) == f"herald_file_{service_name}"
    )

    assert file_handler.maxBytes == max_bytes
    assert file_handler.backupCount == backup_count

    # Test file log write
    test_logger = logging.getLogger("herald.test")
    test_logger.info("Test message for worker-test")

    file_handler.flush()
    log_file = log_dir / f"{service_name}.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "Test message for worker-test" in content


def test_setup_service_logging_default_policy(tmp_path: Path):
    """Verify setup_service_logging defaults to 5 MB max_bytes and backup_count=1."""
    log_dir = tmp_path / "logs"
    service_name = "default-policy-service"

    setup_service_logging(service_name=service_name, log_dir=str(log_dir))

    root = logging.getLogger()
    file_handler = next(
        h for h in root.handlers if getattr(h, "_herald_handler_id", None) == f"herald_file_{service_name}"
    )

    assert file_handler.maxBytes == 5 * 1024 * 1024
    assert file_handler.backupCount == 1


def test_service_logging_rotation_strictly_one_backup(tmp_path: Path):
    """Verify multiple rotations retain strictly service.log and service.log.1, never service.log.2."""
    log_dir = tmp_path / "logs"
    service_name = "rotation-service"

    setup_service_logging(
        service_name=service_name,
        log_dir=str(log_dir),
        max_bytes=150,
        backup_count=1,
    )

    test_logger = logging.getLogger("herald.rotation.test")
    for i in range(25):
        test_logger.info(f"Line number {i:03d} with extra padding text to exceed byte limits rapidly.")

    root = logging.getLogger()
    for h in root.handlers:
        h.flush()

    main_log = log_dir / f"{service_name}.log"
    backup_1 = log_dir / f"{service_name}.log.1"
    backup_2 = log_dir / f"{service_name}.log.2"

    assert main_log.exists()
    assert backup_1.exists()
    assert not backup_2.exists()


def test_service_logging_redacts_registered_secrets(tmp_path: Path):
    log_dir = tmp_path / "logs"
    service_name = "bot-test"

    clear_registered_secrets()
    secret_key = "super_secret_herald_token_12345"
    register_secret(secret_key)

    setup_service_logging(service_name=service_name, log_dir=str(log_dir))

    test_logger = logging.getLogger("herald.bot.test")
    test_logger.info("Connecting with token %s and key %s", secret_key, "plain_val")

    log_file = log_dir / f"{service_name}.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")

    assert secret_key not in content
    assert "[REDACTED]" in content
    assert "plain_val" in content


def test_setup_service_logging_graceful_fallback(tmp_path: Path, capsys):
    service_name = "fallback-service"

    # Simulate an unwritable directory by mocking mkdir to raise PermissionError
    with patch.object(Path, "mkdir", side_effect=PermissionError("Permission denied: /unwritable/logs")):
        setup_service_logging(service_name=service_name, log_dir="/unwritable/logs")

    captured = capsys.readouterr()
    assert "Could not initialize persistent file logging" in captured.err
    assert "Permission denied" in captured.err
