import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import herald.db.connection as db_conn
import herald.db.models  # noqa: F401
from herald.db.connection import Base

db_url = os.getenv("HERALD_TEST_DATABASE_URL")

if db_url:
    test_engine = create_engine(db_url)
else:
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

TestingSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
db_conn.engine = test_engine
db_conn.SessionLocal = TestingSessionLocal

Base.metadata.create_all(bind=test_engine)



@pytest.fixture(scope="function", autouse=True)
def db_session():
    """Provides a clean transactional database session for each test function."""
    Base.metadata.create_all(bind=test_engine)
    session = TestingSessionLocal()

    try:
        yield session
    finally:
        session.close()
        with test_engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
