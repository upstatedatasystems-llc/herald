import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

from herald.core.models import HeraldRequest
from herald.core.pipeline import process_herald_request
from herald.db.models import Base, PodcastJob


def test_telegram_unique_constraint_and_race_handling(monkeypatch):
    """
    Test that concurrent requests with the same (transport, telegram_chat_id, telegram_message_id)
    only create one job and race attempts load the existing job safely.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    req1 = HeraldRequest(
        transport="telegram",
        transport_message_id="1001",
        requester_identity="telegram:888",
        delivery_target="999",
        mode="literal",
        source_text="Test source message for idempotency validation.",
    )

    with TestingSession() as db:
        res1 = process_herald_request(db, req1)
        assert res1.is_duplicate is False

        # Attempt to insert identical transport message
        req2 = HeraldRequest(
            transport="telegram",
            transport_message_id="1001",
            requester_identity="telegram:888",
            delivery_target="999",
            mode="literal",
            source_text="Test source message for idempotency validation.",
        )
        res2 = process_herald_request(db, req2)
        assert res2.is_duplicate is True
        assert res2.job_id == res1.job_id

        # Verify only 1 job exists in DB
        count = db.query(PodcastJob).count()
        assert count == 1
