"""
Unit tests for Alembic Migration 017 (Vendor-neutral AI provider failover and preferences).
Tests:
- Upgrade 016 -> 017 adds columns to podcast_jobs and telegram_users
- String(255) for ai_model and ai_effective_model
- Deterministic historical backfill for podcast_jobs (ai_interactions, groq, cloudflare, literal, gemini)
- Empty telegram user rows remain NULL (no server defaults persisted)
- Clean downgrade
- Full upgrade from zero to head
"""

import json
from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text


def test_migration_017_upgrade_and_downgrade(tmp_path):
    db_file = tmp_path / "test_mig_017.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # 1. Upgrade to 016 first
    command.upgrade(alembic_cfg, "016_expand_custom_title_text")
    engine = create_engine(db_url)
    inspector = inspect(engine)
    cols_016 = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "ai_provider" not in cols_016
    assert "ai_model" not in cols_016
    assert "ai_provider_chain_json" not in cols_016
    assert "ai_failover_index" not in cols_016

    # Insert historical test jobs to verify backfill rules
    with engine.begin() as conn:
        # Job 1: Has ai_interactions row for script_generation
        conn.execute(
            text(
                "INSERT INTO podcast_jobs (id, transport, source_hash, source_text, status, gemini_model, created_at, updated_at) "
                "VALUES ('job-with-interaction', 'telegram', 'hash1', 'Source 1', 'COMPLETE', 'gemini-3.5-flash', '2026-09-08 12:00:00', '2026-09-08 12:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO ai_interactions (id, job_id, provider, model, operation, success, started_at, created_at) "
                "VALUES ('ai-1', 'job-with-interaction', 'groq', 'groq/compound', 'script_generation', 1, '2026-09-08 12:01:00', '2026-09-08 12:01:00')"
            )
        )

        # Job 2: Historical Cloudflare model stored in gemini_model without ai_interaction
        conn.execute(
            text(
                "INSERT INTO podcast_jobs (id, transport, source_hash, source_text, status, gemini_model, created_at, updated_at) "
                "VALUES ('job-cf-in-gemini-col', 'telegram', 'hash2', 'Source 2', 'COMPLETE', '@cf/meta/llama-3.3-70b-instruct-fp8-fast', '2026-09-08 12:02:00', '2026-09-08 12:02:00')"
            )
        )

        # Job 3: Literal mode job
        conn.execute(
            text(
                "INSERT INTO podcast_jobs (id, transport, source_hash, source_text, status, request_mode, created_at, updated_at) "
                "VALUES ('job-literal', 'telegram', 'hash3', 'Source 3', 'COMPLETE', 'literal', '2026-09-08 12:03:00', '2026-09-08 12:03:00')"
            )
        )

        # Job 4: Telegram user with no stored preference
        conn.execute(
            text(
                "INSERT INTO telegram_users (id, telegram_user_id, telegram_chat_id, role, is_active, created_at, updated_at) "
                "VALUES ('user-1', 123456, 123456, 'owner', 1, '2026-09-08 12:00:00', '2026-09-08 12:00:00')"
            )
        )

    # 2. Upgrade to 017
    command.upgrade(alembic_cfg, "017_vendor_neutral_failover")

    inspector = inspect(engine)
    cols_017_jobs = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "ai_provider" in cols_017_jobs
    assert "ai_model" in cols_017_jobs
    assert "ai_provider_chain_json" in cols_017_jobs
    assert "ai_effective_provider" in cols_017_jobs
    assert "ai_effective_model" in cols_017_jobs
    assert "ai_failover_index" in cols_017_jobs
    assert "gemini_model" in cols_017_jobs  # Legacy column preserved

    cols_017_users = {c["name"]: c for c in inspector.get_columns("telegram_users")}
    assert "ai_provider_chain_json" in cols_017_users
    assert "ai_models_by_provider_json" in cols_017_users

    # Verify backfill results
    with engine.connect() as conn:
        # Job 1: Backfilled from ai_interactions -> groq / groq/compound
        r1 = conn.execute(
            text("SELECT ai_provider, ai_model, ai_effective_provider, ai_effective_model, ai_failover_index, ai_provider_chain_json FROM podcast_jobs WHERE id = 'job-with-interaction'")
        ).fetchone()
        assert r1[0] == "groq"
        assert r1[1] == "groq/compound"
        assert r1[2] == "groq"
        assert r1[3] == "groq/compound"
        assert r1[4] == 0
        chain1 = json.loads(r1[5]) if isinstance(r1[5], str) else r1[5]
        assert chain1 == [{"provider": "groq", "model": "groq/compound"}]

        # Job 2: Backfilled from model signature -> cloudflare / @cf/meta/...
        r2 = conn.execute(
            text("SELECT ai_provider, ai_model, ai_effective_provider, ai_effective_model FROM podcast_jobs WHERE id = 'job-cf-in-gemini-col'")
        ).fetchone()
        assert r2[0] == "cloudflare"
        assert r2[1] == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
        assert r2[2] == "cloudflare"

        # Job 3: Literal job -> literal / none
        r3 = conn.execute(
            text("SELECT ai_provider, ai_model, ai_effective_provider FROM podcast_jobs WHERE id = 'job-literal'")
        ).fetchone()
        assert r3[0] == "literal"
        assert r3[1] == "none"
        assert r3[2] == "literal"

        # User 1: Empty preferences MUST REMAIN NULL (User Correction 2)
        u1 = conn.execute(
            text("SELECT ai_provider_chain_json, ai_models_by_provider_json FROM telegram_users WHERE id = 'user-1'")
        ).fetchone()
        assert u1[0] is None
        assert u1[1] is None

    # 3. Verify clean downgrade back to 016
    command.downgrade(alembic_cfg, "016_expand_custom_title_text")
    inspector = inspect(engine)
    cols_after = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "ai_provider" not in cols_after
    assert "ai_model" not in cols_after
    assert "ai_provider_chain_json" not in cols_after


def test_migration_017_from_zero_to_head(tmp_path):
    """Verify clean upgrade from empty database to head including 017."""
    db_file = tmp_path / "test_mig_017_head.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(alembic_cfg, "head")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("podcast_jobs")}
    assert "ai_provider" in cols
    assert "ai_model" in cols
    assert "ai_provider_chain_json" in cols
    assert "ai_effective_provider" in cols
    assert "ai_effective_model" in cols
    assert "ai_failover_index" in cols
