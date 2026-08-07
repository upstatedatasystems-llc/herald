from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text as sa_text

from apps.api.main import app
from herald.config import settings
from herald.db.connection import Base

client = TestClient(app)


def test_clean_alembic_migration_and_idempotency(tmp_path):
    """
    Test clean database table initialization:
    1. Empty database.
    2. Create schema using Base.metadata.create_all on clean engine.
    3. Confirm expected tables exist.
    4. Confirm running create_all a second time is idempotent.
    """
    db_file = tmp_path / "test_clean_deploy.db"
    db_url = f"sqlite:///{db_file}"

    engine = create_engine(db_url)
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    tables = inspector.get_table_names()

    assert "podcast_jobs" in tables
    assert "job_state_transitions" in tables

    # Idempotent second run
    Base.metadata.create_all(bind=engine)
    assert "podcast_jobs" in inspector.get_table_names()


def test_all_alembic_revisions_fit_in_varchar_32():
    """
    Assert that all Alembic revision identifiers in migrations/versions/
    are <= 32 characters long, ensuring compatibility with PostgreSQL's
    alembic_version.version_num VARCHAR(32) column length limit.
    """
    from alembic.script import ScriptDirectory
    from alembic.config import Config

    alembic_cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(alembic_cfg)

    revisions = [sc.revision for sc in script.walk_revisions()]
    assert len(revisions) > 0

    for rev_id in revisions:
        assert len(rev_id) <= 32, f"Revision identifier '{rev_id}' ({len(rev_id)} chars) exceeds VARCHAR(32) limit!"


def test_alembic_004_to_005_migration_with_existing_completed_jobs(tmp_path, monkeypatch):
    """
    Test running Alembic migration 004 -> 005 on a database with pre-existing COMPLETE jobs,
    verifying PostgreSQL VARCHAR(32) compatibility, resulting revision '005_drive_artifacts',
    and schema integrity.
    """
    from alembic.config import Config
    from alembic import command
    from sqlalchemy.orm import sessionmaker

    db_file = tmp_path / "test_migration_004_005.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setattr(settings, "DATABASE_URL", db_url)

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # 1. Upgrade up to revision 004
    command.upgrade(alembic_cfg, "004_align_orm_schema")

    # 2. Emulate PostgreSQL alembic_version table behavior with VARCHAR(32)
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Enforce current version check
    current_ver = session.execute(sa_text("SELECT version_num FROM alembic_version")).scalar()
    assert current_ver == "004_align_orm_schema"
    assert len(current_ver) <= 32

    # Insert a COMPLETE job into the 004 database schema
    session.execute(
        sa_text(
            """
            INSERT INTO podcast_jobs (
                id, gmail_message_id, sender_email, request_mode, source_type,
                source_hash, source_text, status, completed_chunk_index,
                synthesis_attempt_count, delivery_attempt_count, audio_sha256,
                created_at, updated_at, completed_at
            ) VALUES (
                'live-job-001', 'msg-live-1', 'user@example.com', 'standard', 'email_body',
                'hash-live-1', 'Source text', 'COMPLETE', 5,
                1, 1, 'sha256abc',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        )
    )
    session.commit()
    session.close()

    # 3. Upgrade to head (005_drive_artifacts)
    command.upgrade(alembic_cfg, "head")

    # 4. Verify resulting Alembic version is exactly '005_drive_artifacts' and fits in VARCHAR(32)
    session = Session()
    new_ver = session.execute(sa_text("SELECT version_num FROM alembic_version")).scalar()
    assert new_ver == "005_drive_artifacts"
    assert len(new_ver) <= 32

    # 5. Verify columns exist and live job record is preserved cleanly
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("podcast_jobs")}
    assert "source_drive_file_id" in cols
    assert "source_drive_web_link" in cols
    assert "diagnostics_drive_file_id" in cols
    assert "diagnostics_drive_web_link" in cols
    assert "audio_ready_at" in cols
    assert "kokoro_voice" in cols
    assert "kokoro_speed" in cols
    assert "gemini_model" in cols

    job_row = session.execute(
        sa_text("SELECT id, status, completed_chunk_index FROM podcast_jobs WHERE id='live-job-001'")
    ).fetchone()
    assert job_row[0] == "live-job-001"
    assert job_row[1] == "COMPLETE"
    assert job_row[2] == 5
    session.close()
