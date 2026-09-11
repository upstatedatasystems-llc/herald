"""
Unit tests for Alembic Migration 015 (rerun lineage, generation settings snapshot, progress claim, and auto diagnostics).
Tests upgrade from 014 -> 015, column structure, indexes, survival of existing data, multi-job source_hash support,
transport uniqueness preservation, and clean downgrade.
"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_migration_015_upgrade_and_downgrade(tmp_path):
    """Verify migration 015 adds columns, preserves existing jobs, and downgrades cleanly."""
    db_file = tmp_path / "test_mig_015.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # 1. Upgrade to 014 first
    command.upgrade(alembic_cfg, "014_diag_events")
    engine = create_engine(db_url)
    inspector = inspect(engine)
    cols_014 = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "rerun_of_job_id" not in cols_014
    assert "generation_settings_json" not in cols_014

    # Insert a dummy row into podcast_jobs to verify it survives upgrade
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO podcast_jobs (id, transport, source_hash, source_text, status, created_at, updated_at) "
                "VALUES ('job-legacy-014', 'telegram', 'hash_legacy_123', 'Legacy source text', 'COMPLETE', '2026-09-08 12:00:00', '2026-09-08 12:00:00')"
            )
        )

    # 2. Upgrade to 015
    command.upgrade(alembic_cfg, "015_rerun_lineage_diagnostics")

    inspector = inspect(engine)
    cols_015 = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "rerun_of_job_id" in cols_015
    assert "generation_settings_json" in cols_015
    assert "first_chunk_progress_claimed_at" in cols_015
    assert "telegram_progress_message_id" in cols_015
    assert "auto_diagnostics_json" in cols_015

    # Verify existing legacy row survived with nulls for new columns
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, source_hash, status, rerun_of_job_id FROM podcast_jobs WHERE id = 'job-legacy-014'")
        ).fetchone()
        assert row is not None
        assert row[0] == "job-legacy-014"
        assert row[1] == "hash_legacy_123"
        assert row[2] == "COMPLETE"
        assert row[3] is None

    # 3. Prove two genuinely different requests CAN create two jobs with the same source_hash
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO podcast_jobs (id, transport, telegram_chat_id, telegram_message_id, source_hash, source_text, status, rerun_of_job_id, created_at, updated_at) "
                "VALUES ('job-new-1', 'telegram', 100, 1001, 'shared_hash_abc', 'Shared source text', 'AWAITING_APPROVAL', 'job-legacy-014', '2026-09-08 12:01:00', '2026-09-08 12:01:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO podcast_jobs (id, transport, telegram_chat_id, telegram_message_id, source_hash, source_text, status, rerun_of_job_id, created_at, updated_at) "
                "VALUES ('job-new-2', 'telegram', 100, 1002, 'shared_hash_abc', 'Shared source text', 'QUEUED_TTS', 'job-new-1', '2026-09-08 12:02:00', '2026-09-08 12:02:00')"
            )
        )

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM podcast_jobs WHERE source_hash = 'shared_hash_abc'")
        ).scalar()
        assert count == 2

    # 4. Prove same Telegram transport request CANNOT create two jobs (transport uniqueness remains)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO podcast_jobs (id, transport, telegram_chat_id, telegram_message_id, source_hash, source_text, status, created_at, updated_at) "
                    "VALUES ('job-dup-transport', 'telegram', 100, 1001, 'another_hash', 'Other text', 'RECEIVED', '2026-09-08 12:03:00', '2026-09-08 12:03:00')"
                )
            )

    # 5. Verify downgrade from 015 back to 014
    command.downgrade(alembic_cfg, "014_diag_events")
    inspector = inspect(engine)
    cols_after = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "rerun_of_job_id" not in cols_after
    assert "generation_settings_json" not in cols_after
    assert "first_chunk_progress_claimed_at" not in cols_after
    assert "telegram_progress_message_id" not in cols_after
    assert "auto_diagnostics_json" not in cols_after


def test_migration_015_from_zero_to_head(tmp_path):
    """Verify clean upgrade from empty database to head (including 015)."""
    db_file = tmp_path / "test_mig_015_head.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(alembic_cfg, "head")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "rerun_of_job_id" in cols
    assert "generation_settings_json" in cols
    assert "first_chunk_progress_claimed_at" in cols
    assert "telegram_progress_message_id" in cols
    assert "auto_diagnostics_json" in cols
