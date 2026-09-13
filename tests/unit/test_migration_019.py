"""Unit tests for Alembic Migration 019 (Long-form telemetry and crash recovery).
Tests:
- Upgrade 018 -> 019 adds research_provider and research_model columns to podcast_jobs
- Clean downgrade to 018 drops the columns
- Full upgrade to head succeeds
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_migration_019_upgrade_and_downgrade(tmp_path):
    db_file = tmp_path / "test_mig_019.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # 1. Upgrade to 018 first
    command.upgrade(alembic_cfg, "018_interactive_params_longform")
    engine = create_engine(db_url)
    inspector = inspect(engine)
    cols_018 = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "research_provider" not in cols_018
    assert "research_model" in cols_018

    # Insert test job
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO podcast_jobs (id, transport, source_hash, source_text, status, request_mode, created_at, updated_at) "
                "VALUES ('job-019-test', 'telegram', 'hash019', 'Source 019', 'COMPLETE', 'research', '2026-09-13 12:00:00', '2026-09-13 12:00:00')"
            )
        )

    # 2. Upgrade to 019
    command.upgrade(alembic_cfg, "019_longform_fixes_recovery")

    inspector = inspect(engine)
    cols_019 = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "research_provider" in cols_019

    # Verify write and read
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE podcast_jobs SET research_provider = 'gemini' "
                "WHERE id = 'job-019-test'"
            )
        )
        row = conn.execute(
            text("SELECT research_provider FROM podcast_jobs WHERE id = 'job-019-test'")
        ).fetchone()
        assert row[0] == "gemini"

    # 3. Test downgrade back to 018
    command.downgrade(alembic_cfg, "018_interactive_params_longform")
    inspector = inspect(engine)
    cols_downgraded = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "research_provider" not in cols_downgraded

    # 4. Re-upgrade to head
    command.upgrade(alembic_cfg, "head")
    inspector = inspect(engine)
    cols_head = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "research_provider" in cols_head
