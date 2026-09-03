from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import PodcastJob


@pytest.fixture
def alembic_config(tmp_path, monkeypatch):
    """Create an Alembic Config instance pointing to a temporary SQLite database."""
    db_path = tmp_path / "test_migration_012.db"
    db_url = f"sqlite:///{db_path}"

    monkeypatch.setattr(settings, "DATABASE_URL", db_url)

    ini_path = Path("alembic.ini").resolve()
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(Path("migrations").resolve()))

    return cfg, db_url


def test_migration_012_empty_to_head(alembic_config):
    """Test clean migration from empty database to latest revision (head)."""
    cfg, db_url = alembic_config
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    inspector = inspect(engine)

    # Check podcast_jobs columns
    columns = {col["name"]: col for col in inspector.get_columns("podcast_jobs")}
    assert "approval_required" in columns
    assert "approval_requested_at" in columns
    assert "approved_at" in columns
    assert "telegram_approval_message_id" in columns
    assert "telegram_delivery_message_id" in columns
    assert "telegram_audio_file_id" in columns
    assert "telegram_document_file_id" in columns


def test_migration_012_from_revision_011(alembic_config):
    """
    Test upgrading from revision 011 to 012 with existing rows.
    Verifies that existing jobs get safe defaults (approval_required=False, NULL timestamps/file IDs).
    """
    cfg, db_url = alembic_config

    # 1. Upgrade to revision 011
    command.upgrade(cfg, "011_telegram_user_preferences")

    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO podcast_jobs (id, transport, source_hash, source_text, status, attempt_count, "
                "synthesis_attempt_count, delivery_attempt_count, verify_repair_count, research_repair_count, "
                "completed_chunk_index, created_at, updated_at) "
                "VALUES ('job-uuid-1', 'telegram', 'hash123', 'Sample text', 'QUEUED_TTS', 0, 0, 0, 0, 0, 0, "
                "'2026-09-01 00:00:00', '2026-09-01 00:00:00')"
            )
        )

    # 2. Upgrade to revision 012
    command.upgrade(cfg, "012_telegram_approval_delivery")

    # 3. Query upgraded row and verify defaults
    Session = sessionmaker(bind=engine)
    with Session() as db:
        job = db.query(PodcastJob).filter_by(id="job-uuid-1").first()
        assert job is not None
        assert job.approval_required is False or job.approval_required == 0
        assert job.approval_requested_at is None
        assert job.approved_at is None
        assert job.telegram_approval_message_id is None
        assert job.telegram_delivery_message_id is None
        assert job.telegram_audio_file_id is None
        assert job.telegram_document_file_id is None


def test_migration_012_from_revision_010(alembic_config):
    """Test upgrading from 010 straight to head."""
    cfg, db_url = alembic_config
    command.upgrade(cfg, "010_telegram_and_literal_support")
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    columns = {col["name"]: col for col in inspector.get_columns("podcast_jobs")}
    assert "approval_required" in columns
    assert "telegram_audio_file_id" in columns


def test_migration_012_downgrade(alembic_config):
    """Test downgrade from 012 to 011 removes approval/delivery columns cleanly."""
    cfg, db_url = alembic_config

    command.upgrade(cfg, "012_telegram_approval_delivery")
    command.downgrade(cfg, "011_telegram_user_preferences")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("podcast_jobs")}
    assert "approval_required" not in columns
    assert "telegram_approval_message_id" not in columns
    assert "telegram_audio_file_id" not in columns
