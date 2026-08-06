import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Set environment variable for mock TTS during testing
os.environ["HERALD_MOCK_TTS"] = "1"
os.environ["HERALD_ENV"] = "testing"

from packages.herald.db.connection import Base


@pytest.fixture(scope="function")
def db_session():
    """Create an in-memory SQLite database session for unit testing."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
