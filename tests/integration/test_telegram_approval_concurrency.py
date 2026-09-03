import concurrent.futures
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.core.models import HeraldRequest
from herald.core.pipeline import process_herald_request
from herald.db.models import Base, JobState, JobStateTransition, PodcastJob
from herald.telegram.auth import generate_pairing_code, verify_and_claim_pairing_code
from herald.telegram.bot import handle_telegram_callback_query
from herald.telegram.client import TelegramClient


@pytest.fixture
def db_factory(tmp_path):
    db_file = tmp_path / "concurrency_approval.db"
    engine = create_engine(
        f"sqlite:///{db_file}?timeout=30",
        connect_args={"check_same_thread": False},
    )
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    return TestingSession


def test_concurrent_approve_vs_approve(db_factory):
    """
    Two concurrent sessions attempting to approve the same AWAITING_APPROVAL job.
    Exactly one transitions the job, and the state history records exactly one AWAITING_APPROVAL -> QUEUED_TTS transition.
    """
    with db_factory() as db:
        code = generate_pairing_code(db)
        verify_and_claim_pairing_code(db, code, user_id=12345, chat_id=12345, username="owner")

        req = HeraldRequest(
            transport="telegram",
            transport_message_id=301,
            requester_identity="telegram:12345",
            delivery_target=12345,
            request_mode="literal",
            source_text="Test concurrent approve source.",
            hold_for_approval=True,
        )
        res = process_herald_request(db, req)
        job_id = res.job_id

    mock_client1 = MagicMock(spec=TelegramClient)
    mock_client2 = MagicMock(spec=TelegramClient)

    def worker_approve(client, query_id):
        with db_factory() as session:
            cb_query = {
                "id": query_id,
                "from": {"id": 12345},
                "message": {"message_id": 901, "chat": {"id": 12345, "type": "private"}},
                "data": f"h2:approve:{job_id}",
            }
            handle_telegram_callback_query(session, client, cb_query)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker_approve, mock_client1, "cb-race-1")
        f2 = executor.submit(worker_approve, mock_client2, "cb-race-2")
        f1.result()
        f2.result()

    with db_factory() as db:
        job = db.query(PodcastJob).filter_by(id=job_id).first()
        assert job.status == JobState.QUEUED_TTS.value
        assert job.approved_at is not None

        # Check transitions: exactly ONE transition to QUEUED_TTS
        transitions = (
            db.query(JobStateTransition)
            .filter_by(job_id=job_id, to_state=JobState.QUEUED_TTS.value)
            .all()
        )
        assert len(transitions) == 1


def test_concurrent_approve_vs_cancel(db_factory):
    """
    Concurrent Approve vs Cancel on two sessions: exactly one terminal state wins.
    """
    with db_factory() as db:
        code = generate_pairing_code(db)
        verify_and_claim_pairing_code(db, code, user_id=12345, chat_id=12345, username="owner")

        req = HeraldRequest(
            transport="telegram",
            transport_message_id=302,
            requester_identity="telegram:12345",
            delivery_target=12345,
            request_mode="literal",
            source_text="Test concurrent approve vs cancel source.",
            hold_for_approval=True,
        )
        res = process_herald_request(db, req)
        job_id = res.job_id

    mock_client_app = MagicMock(spec=TelegramClient)
    mock_client_den = MagicMock(spec=TelegramClient)

    def worker_action(client, action, query_id):
        with db_factory() as session:
            cb_query = {
                "id": query_id,
                "from": {"id": 12345},
                "message": {"message_id": 902, "chat": {"id": 12345, "type": "private"}},
                "data": f"h2:{action}:{job_id}",
            }
            handle_telegram_callback_query(session, client, cb_query)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker_action, mock_client_app, "approve", "cb-app-race")
        f2 = executor.submit(worker_action, mock_client_den, "deny", "cb-den-race")
        concurrent.futures.wait([f1, f2])

    with db_factory() as db:
        job = db.query(PodcastJob).filter_by(id=job_id).first()
        # Exactly one state won
        assert job.status in (JobState.QUEUED_TTS.value, JobState.CANCELLED.value)

        # There must NOT be conflicting multiple decisions
        if job.status == JobState.QUEUED_TTS.value:
            assert job.approved_at is not None
        elif job.status == JobState.CANCELLED.value:
            assert job.approved_at is None
