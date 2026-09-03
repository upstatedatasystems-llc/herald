"""
Tests for database migration 013 (ai_interactions table and diagnostics support).
Verifies upgrading from scratch, from 012 -> 013, table schema, indexes, and downgrade.
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_migration_013_upgrade_and_downgrade(tmp_path):
    db_file = tmp_path / "test_mig_013.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # 1. Upgrade to 012 first
    command.upgrade(alembic_cfg, "012_telegram_approval_delivery")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    tables_012 = inspector.get_table_names()
    assert "podcast_jobs" in tables_012
    assert "ai_interactions" not in tables_012

    # 2. Upgrade to 013
    command.upgrade(alembic_cfg, "013_ai_interactions")

    inspector = inspect(engine)
    tables_013 = inspector.get_table_names()
    assert "ai_interactions" in tables_013

    # Check columns
    cols = {c["name"]: c for c in inspector.get_columns("ai_interactions")}
    expected_cols = [
        "id",
        "job_id",
        "provider",
        "model",
        "operation",
        "started_at",
        "completed_at",
        "duration_ms",
        "success",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "error_category",
        "error_message",
        "metadata_json",
        "created_at",
    ]
    for col_name in expected_cols:
        assert col_name in cols, f"Missing column '{col_name}' in ai_interactions table"

    # Check indexes
    indexes = {idx["name"] for idx in inspector.get_indexes("ai_interactions")}
    assert "idx_ai_interactions_job_created" in indexes
    assert "idx_ai_interactions_provider_created" in indexes

    # 3. Downgrade back to 012
    command.downgrade(alembic_cfg, "012_telegram_approval_delivery")

    inspector = inspect(engine)
    tables_downgraded = inspector.get_table_names()
    assert "ai_interactions" not in tables_downgraded
    assert "podcast_jobs" in tables_downgraded


def test_migration_013_from_empty_to_head(tmp_path):
    db_file = tmp_path / "test_mig_013_head.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # Upgrade straight to head
    command.upgrade(alembic_cfg, "head")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    assert "ai_interactions" in inspector.get_table_names()
    assert "podcast_jobs" in inspector.get_table_names()
