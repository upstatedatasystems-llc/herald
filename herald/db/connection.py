from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from herald.config import settings

Base = declarative_base()

db_url = settings.get_database_url()

# For SQLite testing fallback or Postgres production
engine = create_engine(
    db_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
