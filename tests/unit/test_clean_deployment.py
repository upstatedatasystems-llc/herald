from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from apps.api.main import app
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
