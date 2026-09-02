import importlib.util
import pathlib
from unittest.mock import MagicMock

import pytest


def test_migration_downgrade_fails_safely_when_telegram_rows_exist(monkeypatch):
    """
    Test that migration 010 downgrade safely detects Telegram-era rows with NULL gmail_message_id
    and raises an actionable RuntimeError rather than an unhandled SQL error.
    """
    mock_op = MagicMock()
    mock_bind = MagicMock()
    mock_op.get_bind.return_value = mock_bind

    # Mock DB query returning 3 rows with NULL gmail_message_id
    mock_bind.execute.return_value.scalar.return_value = 3

    p = pathlib.Path("migrations/versions/010_telegram_and_literal_support.py").resolve()
    spec = importlib.util.spec_from_file_location("migration_010", p)
    mig_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig_mod)
    monkeypatch.setattr(mig_mod, "op", mock_op)

    with pytest.raises(RuntimeError, match="Cannot downgrade database"):
        mig_mod.downgrade()
