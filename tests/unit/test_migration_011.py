import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import TelegramUser
from herald.telegram.auth import get_effective_user_preferences


@pytest.fixture
def alembic_config(tmp_path, monkeypatch):
    """Create an Alembic Config instance pointing to a temporary SQLite database."""
    db_path = tmp_path / "test_migration_011.db"
    db_url = f"sqlite:///{db_path}"

    monkeypatch.setattr(settings, "DATABASE_URL", db_url)

    ini_path = Path("alembic.ini").resolve()
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(Path("migrations").resolve()))

    return cfg, db_url


def test_migration_011_empty_to_head(alembic_config):
    """Test clean migration from empty database to latest revision (head)."""
    cfg, db_url = alembic_config
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    inspector = inspect(engine)

    # Check telegram_users columns
    columns = {col["name"]: col for col in inspector.get_columns("telegram_users")}
    assert "confirm_before_tts" in columns
    assert "default_voice" in columns
    assert "default_speed" in columns
    assert "default_mode" in columns


def test_migration_011_from_revision_010(alembic_config):
    """
    Test upgrading from Phase 1 revision 010 to 011 with existing Phase 1 rows.
    Verifies that existing users get safe defaults (confirm_before_tts=False, NULL prefs).
    """
    cfg, db_url = alembic_config

    # 1. Upgrade to revision 010
    command.upgrade(cfg, "010_telegram_and_literal_support")

    engine = create_engine(db_url)
    with engine.begin() as conn:
        # Insert a Phase 1 Telegram user row
        conn.execute(
            text(
                "INSERT INTO telegram_users (id, telegram_user_id, telegram_chat_id, username, first_name, role, is_active, created_at, updated_at) "
                "VALUES ('user-uuid-1', 98765, 98765, 'legacy_owner', 'Legacy', 'owner', 1, '2026-09-01 00:00:00', '2026-09-01 00:00:00')"
            )
        )

    # 2. Upgrade to revision 011
    command.upgrade(cfg, "011_telegram_user_preferences")

    # 3. Query upgraded row and verify defaults
    Session = sessionmaker(bind=engine)
    with Session() as db:
        user = db.query(TelegramUser).filter_by(telegram_user_id=98765).first()
        assert user is not None
        assert user.confirm_before_tts is False or user.confirm_before_tts == 0
        assert user.default_voice is None
        assert user.default_speed is None
        assert user.default_mode is None

        # Verify fallback to instance settings
        prefs = get_effective_user_preferences(db, 98765)
        assert prefs["confirm_before_tts"] is False
        assert prefs["default_voice"] == settings.KOKORO_VOICE
        assert prefs["default_speed"] == float(settings.KOKORO_SPEED)
        assert prefs["default_mode"] == settings.get_default_mode()


def test_migration_011_downgrade(alembic_config):
    """Test downgrade from 011 to 010 removes preference columns cleanly."""
    cfg, db_url = alembic_config

    # Upgrade to 011
    command.upgrade(cfg, "011_telegram_user_preferences")

    # Downgrade to 010
    command.downgrade(cfg, "010_telegram_and_literal_support")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("telegram_users")}

    assert "confirm_before_tts" not in columns
    assert "default_voice" not in columns
    assert "default_speed" not in columns
    assert "default_mode" not in columns
    assert "telegram_user_id" in columns
