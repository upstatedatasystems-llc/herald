"""Unit tests for Alembic Migration 018 (Interactive podcast parameters and long-form).
Tests:
- Upgrade 017 -> 018 adds columns to podcast_jobs and telegram_users
- Backfills content_mode and target_minutes for historical jobs
- Default columns in telegram_users
- Clean downgrade
- Full upgrade to head
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_migration_018_upgrade_and_downgrade(tmp_path):
    db_file = tmp_path / "test_mig_018.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # 1. Upgrade to 017 first
    command.upgrade(alembic_cfg, "017_vendor_neutral_failover")
    engine = create_engine(db_url)
    inspector = inspect(engine)
    cols_017 = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "content_mode" not in cols_017
    assert "target_minutes" not in cols_017
    assert "research_plan_json" not in cols_017

    # Insert historical test jobs to verify backfill rules
    with engine.begin() as conn:
        # Job 1: Standard mode job
        conn.execute(
            text(
                "INSERT INTO podcast_jobs (id, transport, source_hash, source_text, status, request_mode, created_at, updated_at) "
                "VALUES ('job-standard', 'telegram', 'hash1', 'Source 1', 'COMPLETE', 'standard', '2026-09-12 12:00:00', '2026-09-12 12:00:00')"
            )
        )
        # Job 2: Literal mode job
        conn.execute(
            text(
                "INSERT INTO podcast_jobs (id, transport, source_hash, source_text, status, request_mode, created_at, updated_at) "
                "VALUES ('job-literal', 'telegram', 'hash2', 'Source 2', 'COMPLETE', 'literal', '2026-09-12 12:01:00', '2026-09-12 12:01:00')"
            )
        )
        # Job 3: Research mode job
        conn.execute(
            text(
                "INSERT INTO podcast_jobs (id, transport, source_hash, source_text, status, request_mode, research_depth, created_at, updated_at) "
                "VALUES ('job-research', 'telegram', 'hash3', 'Source 3', 'COMPLETE', 'research', 'high', '2026-09-12 12:02:00', '2026-09-12 12:02:00')"
            )
        )
        # User 1: Telegram user
        conn.execute(
            text(
                "INSERT INTO telegram_users (id, telegram_user_id, telegram_chat_id, role, is_active, created_at, updated_at) "
                "VALUES ('u-1', 1001, 1001, 'owner', 1, '2026-09-12 12:00:00', '2026-09-12 12:00:00')"
            )
        )

    # 2. Upgrade to 018
    command.upgrade(alembic_cfg, "018_interactive_params_longform")

    inspector = inspect(engine)
    cols_018_job = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "content_mode" in cols_018_job
    assert "target_minutes" in cols_018_job
    assert "research_plan_json" in cols_018_job
    assert "evidence_packet_json" in cols_018_job
    assert "outline_json" in cols_018_job
    assert "section_progress_json" in cols_018_job
    assert "fidelity_audit_json" in cols_018_job
    assert "branding_intro_seconds" in cols_018_job
    assert "branding_outro_seconds" in cols_018_job
    assert "program_duration_seconds" in cols_018_job
    assert "configuration_state_json" in cols_018_job
    assert "resolved_default" in cols_018_job
    assert "telegram_config_message_id" in cols_018_job

    cols_018_user = {c["name"]: c for c in inspector.get_columns("telegram_users")}
    assert "default_content_mode" in cols_018_user
    assert "default_target_minutes" in cols_018_user
    assert "default_research_depth" in cols_018_user

    # Verify backfill results
    with engine.begin() as conn:
        r1 = conn.execute(
            text("SELECT content_mode, target_minutes FROM podcast_jobs WHERE id = 'job-standard'")
        ).fetchone()
        assert r1[0] == "source"
        assert r1[1] == "auto"

        r2 = conn.execute(
            text("SELECT content_mode, target_minutes FROM podcast_jobs WHERE id = 'job-literal'")
        ).fetchone()
        assert r2[0] == "literal"
        assert r2[1] == "auto"

        r3 = conn.execute(
            text("SELECT content_mode, target_minutes, research_depth FROM podcast_jobs WHERE id = 'job-research'")
        ).fetchone()
        assert r3[0] == "expanded"
        assert r3[1] == "auto"
        assert r3[2] == "high"

    # 3. Test downgrade
    command.downgrade(alembic_cfg, "017_vendor_neutral_failover")
    inspector = inspect(engine)
    cols_downgraded = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "content_mode" not in cols_downgraded
    assert "target_minutes" not in cols_downgraded

    # 4. Upgrade back to head
    command.upgrade(alembic_cfg, "head")
    inspector = inspect(engine)
    assert "content_mode" in {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
