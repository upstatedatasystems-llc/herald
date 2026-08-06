import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import herald.db.connection as db_conn
from herald.db.connection import Base

test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestingSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
db_conn.engine = test_engine
db_conn.SessionLocal = TestingSessionLocal

Base.metadata.create_all(bind=test_engine)

from apps.api.main import app
from herald.db.connection import get_db


@pytest.fixture(scope="function", autouse=True)
def db_session():
    """Provides a clean transactional in-memory SQLite database session for each test function."""
    Base.metadata.create_all(bind=test_engine)
    session = TestingSessionLocal()

    def override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    try:
        yield session
    finally:
        session.close()
        app.dependency_overrides.clear()
        with test_engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
