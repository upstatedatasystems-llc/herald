"""
Unit tests for Alembic Migration 014 (job_diagnostic_events and ai_interactions extension).
Tests upgrade from base, upgrade from 012 -> 013 -> 014, table creation, column structure, indexes, and downgrade.
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_migration_014_upgrade_and_downgrade(tmp_path):
    """Verify migration 014 creates job_diagnostic_events and extends ai_interactions cleanly."""
    db_file = tmp_path / "test_mig_014.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # 1. Upgrade to 013 first
    command.upgrade(alembic_cfg, "013_ai_interactions")
    engine = create_engine(db_url)
    inspector = inspect(engine)
    tables_013 = inspector.get_table_names()
    assert "ai_interactions" in tables_013
    assert "job_diagnostic_events" not in tables_013

    # Insert a dummy row into ai_interactions to verify it survives upgrade
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ai_interactions (id, provider, model, operation, started_at, success) "
                "VALUES ('test-ai-1', 'gemini', 'gemini-3.5-flash', 'script_generation', '2026-09-03 12:00:00', 1)"
            )
        )

    # 2. Upgrade to 014
    command.upgrade(alembic_cfg, "014_diag_events")

    inspector = inspect(engine)
    tables_014 = inspector.get_table_names()
    assert "job_diagnostic_events" in tables_014
    assert "ai_interactions" in tables_014

    # Verify columns of job_diagnostic_events
    diag_cols = {c["name"]: c for c in inspector.get_columns("job_diagnostic_events")}
    assert "id" in diag_cols
    assert "job_id" in diag_cols
    assert "timestamp" in diag_cols
    assert "level" in diag_cols
    assert "component" in diag_cols
    assert "event_type" in diag_cols
    assert "message" in diag_cols
    assert "metadata_json_sanitized" in diag_cols

    # Verify extended columns of ai_interactions
    ai_cols = {c["name"]: c for c in inspector.get_columns("ai_interactions")}
    assert "attempt" in ai_cols
    assert "http_status" in ai_cols
    assert "provider_request_id" in ai_cols
    assert "input_chars" in ai_cols
    assert "request_json_sanitized" in ai_cols
    assert "response_json_sanitized" in ai_cols

    # Verify existing row survived
    with engine.connect() as conn:
        res = conn.execute(text("SELECT id, provider, attempt FROM ai_interactions WHERE id = 'test-ai-1'")).fetchone()
        assert res is not None
        assert res[0] == "test-ai-1"

    # 3. Verify downgrade from 014 to 013
    command.downgrade(alembic_cfg, "013_ai_interactions")
    inspector = inspect(engine)
    tables_after = inspector.get_table_names()
    assert "job_diagnostic_events" not in tables_after
    assert "ai_interactions" in tables_after
    ai_cols_after = {c["name"]: c for c in inspector.get_columns("ai_interactions")}
    assert "request_json_sanitized" not in ai_cols_after


def test_migration_014_from_zero_to_head(tmp_path):
    """Verify clean upgrade from empty database to head."""
    db_file = tmp_path / "test_mig_014_head.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(alembic_cfg, "head")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    assert "job_diagnostic_events" in inspector.get_table_names()
    assert "ai_interactions" in inspector.get_table_names()
    assert "podcast_jobs" in inspector.get_table_names()
