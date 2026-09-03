from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.worker.main import claim_next_job
from herald.core.models import HeraldRequest
from herald.core.pipeline import process_herald_request
from herald.db.models import Base, JobState, PodcastJob
from herald.telegram.auth import (
    generate_pairing_code,
    set_user_confirm_before_tts,
    verify_and_claim_pairing_code,
)
from herald.telegram.bot import handle_telegram_callback_query, handle_telegram_content_message
from herald.telegram.client import TelegramAPIError, TelegramClient


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_confirmation_disabled_auto_queues(db_session):
    """When confirmation is disabled, job goes directly to QUEUED_TTS and is claimable."""
    req = HeraldRequest(
        transport="telegram",
        transport_message_id=10,
        requester_identity="telegram:12345",
        delivery_target=12345,
        request_mode="literal",
        source_text="Test source text for confirmation disabled.",
        hold_for_approval=False,
    )
    res = process_herald_request(db_session, req)
    assert res.status == JobState.QUEUED_TTS.value

    job = db_session.query(PodcastJob).filter_by(id=res.job_id).first()
    assert job.status == JobState.QUEUED_TTS.value
    assert job.approval_required is False

    # Worker can claim it
    claimed = claim_next_job(db_session, worker_id="test-worker")
    assert claimed is not None
    assert claimed.id == job.id


def test_confirmation_enabled_holds_for_approval_and_worker_cannot_claim(db_session):
    """When confirmation is enabled, job holds in AWAITING_APPROVAL and worker cannot claim it."""
    req = HeraldRequest(
        transport="telegram",
        transport_message_id=11,
        requester_identity="telegram:12345",
        delivery_target=12345,
        request_mode="literal",
        source_text="Test source text for confirmation enabled.",
        hold_for_approval=True,
    )
    res = process_herald_request(db_session, req)
    assert res.status == JobState.AWAITING_APPROVAL.value

    job = db_session.query(PodcastJob).filter_by(id=res.job_id).first()
    assert job.status == JobState.AWAITING_APPROVAL.value
    assert job.approval_required is True
    # approval_requested_at is NULL until presented via Telegram
    assert job.approval_requested_at is None
    assert job.telegram_approval_message_id is None

    # Worker MUST NOT claim it
    claimed = claim_next_job(db_session, worker_id="test-worker")
    assert claimed is None


def test_telegram_intake_presents_approval_card_and_persists_metadata(db_session):
    """Telegram message intake presents approval card when confirm_before_tts is on."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")
    set_user_confirm_before_tts(db_session, user_id=12345, chat_id=12345, enabled=True)

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.is_configured = True
    mock_client.send_message.return_value = {"message_id": 999}

    msg = {
        "chat": {"id": 12345, "type": "private"},
        "from": {"id": 12345},
        "message_id": 100,
        "text": "literal\n\nSample text for approval card test.",
    }

    handle_telegram_content_message(db_session, mock_client, msg)

    mock_client.send_message.assert_called_once()
    call_kwargs = mock_client.send_message.call_args[1]
    assert "Podcast Ready for Approval" in call_kwargs["text"]
    assert call_kwargs["reply_markup"] is not None

    job = db_session.query(PodcastJob).first()
    assert job.status == JobState.AWAITING_APPROVAL.value
    assert job.telegram_approval_message_id == 999
    assert job.approval_requested_at is not None


def test_telegram_approval_card_send_failure_leaves_unpresented_and_non_synthesizing(db_session):
    """If sending the approval card fails, job remains AWAITING_APPROVAL with NULL message ID and never synthesizes."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")
    set_user_confirm_before_tts(db_session, user_id=12345, chat_id=12345, enabled=True)

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.is_configured = True
    mock_client.send_message.side_effect = TelegramAPIError("Network timeout")

    msg = {
        "chat": {"id": 12345, "type": "private"},
        "from": {"id": 12345},
        "message_id": 101,
        "text": "literal\n\nSample text for send failure test.",
    }

    handle_telegram_content_message(db_session, mock_client, msg)

    job = db_session.query(PodcastJob).first()
    assert job is not None
    assert job.status == JobState.AWAITING_APPROVAL.value
    assert job.telegram_approval_message_id is None
    assert job.approval_requested_at is None

    # Worker still cannot claim
    assert claim_next_job(db_session) is None


def test_approve_callback_transitions_exactly_once_to_queued_tts(db_session):
    """Approve callback transitions job from AWAITING_APPROVAL to QUEUED_TTS and edits message to queued card."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")
    set_user_confirm_before_tts(db_session, user_id=12345, chat_id=12345, enabled=True)

    req = HeraldRequest(
        transport="telegram",
        transport_message_id=200,
        requester_identity="telegram:12345",
        delivery_target=12345,
        request_mode="literal",
        source_text="Test source text for approval callback.",
        hold_for_approval=True,
    )
    res = process_herald_request(db_session, req)
    job_id = res.job_id

    mock_client = MagicMock(spec=TelegramClient)
    cb_query = {
        "id": "cb-approve-1",
        "from": {"id": 12345, "username": "owner"},
        "message": {"message_id": 888, "chat": {"id": 12345, "type": "private"}},
        "data": f"h2:approve:{job_id}",
    }

    # 1. First approval
    handle_telegram_callback_query(db_session, mock_client, cb_query)

    job = db_session.query(PodcastJob).filter_by(id=job_id).first()
    assert job.status == JobState.QUEUED_TTS.value
    assert job.approved_at is not None
    mock_client.answer_callback_query.assert_called_with("cb-approve-1", text="Approved! Queued for synthesis.")
    mock_client.edit_message_text.assert_called_once()
    assert "Podcast Queued for Synthesis" in mock_client.edit_message_text.call_args[1]["text"]

    # 2. Second approval is idempotent
    mock_client.reset_mock()
    handle_telegram_callback_query(db_session, mock_client, cb_query)
    mock_client.answer_callback_query.assert_called_with("cb-approve-1", text="Job is already approved and synthesizing.")
    assert job.status == JobState.QUEUED_TTS.value


def test_deny_callback_cancels_job_and_subsequent_approve_rejected(db_session):
    """Deny callback cancels job, and approving a cancelled job is rejected."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    req = HeraldRequest(
        transport="telegram",
        transport_message_id=201,
        requester_identity="telegram:12345",
        delivery_target=12345,
        request_mode="literal",
        source_text="Test source text for deny callback.",
        hold_for_approval=True,
    )
    res = process_herald_request(db_session, req)
    job_id = res.job_id

    mock_client = MagicMock(spec=TelegramClient)
    cb_deny = {
        "id": "cb-deny-1",
        "from": {"id": 12345, "username": "owner"},
        "message": {"message_id": 888, "chat": {"id": 12345, "type": "private"}},
        "data": f"h2:deny:{job_id}",
    }

    # 1. Deny
    handle_telegram_callback_query(db_session, mock_client, cb_deny)
    job = db_session.query(PodcastJob).filter_by(id=job_id).first()
    assert job.status == JobState.CANCELLED.value
    mock_client.answer_callback_query.assert_called_with("cb-deny-1", text="Generation cancelled.")

    # 2. Double deny is idempotent
    mock_client.reset_mock()
    handle_telegram_callback_query(db_session, mock_client, cb_deny)
    mock_client.answer_callback_query.assert_called_with("cb-deny-1", text="Job already cancelled.")

    # 3. Approve after deny is rejected
    mock_client.reset_mock()
    cb_approve = {
        "id": "cb-approve-2",
        "from": {"id": 12345, "username": "owner"},
        "message": {"message_id": 888, "chat": {"id": 12345, "type": "private"}},
        "data": f"h2:approve:{job_id}",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_approve)
    mock_client.answer_callback_query.assert_called_with("cb-approve-2", text="Job was already cancelled.", show_alert=True)
    assert job.status == JobState.CANCELLED.value


def test_deny_after_approve_cannot_cancel_synthesizing_job(db_session):
    """Denying an already approved/synthesizing job through a stale button is rejected."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    req = HeraldRequest(
        transport="telegram",
        transport_message_id=202,
        requester_identity="telegram:12345",
        delivery_target=12345,
        request_mode="literal",
        source_text="Test source text for stale deny.",
        hold_for_approval=True,
    )
    res = process_herald_request(db_session, req)
    job_id = res.job_id

    mock_client = MagicMock(spec=TelegramClient)

    # 1. Approve
    cb_approve = {
        "id": "cb-app",
        "from": {"id": 12345},
        "message": {"message_id": 888, "chat": {"id": 12345, "type": "private"}},
        "data": f"h2:approve:{job_id}",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_approve)
    job = db_session.query(PodcastJob).filter_by(id=job_id).first()
    assert job.status == JobState.QUEUED_TTS.value

    # 2. Stale Deny
    mock_client.reset_mock()
    cb_deny = {
        "id": "cb-deny",
        "from": {"id": 12345},
        "message": {"message_id": 888, "chat": {"id": 12345, "type": "private"}},
        "data": f"h2:deny:{job_id}",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_deny)
    mock_client.answer_callback_query.assert_called_with("cb-deny", text="Job is already synthesizing and cannot be cancelled.", show_alert=True)
    assert job.status == JobState.QUEUED_TTS.value


def test_callback_unauthorized_user_or_wrong_chat_rejected(db_session):
    """Callbacks from wrong user or wrong chat are rejected without leaking job information."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    req = HeraldRequest(
        transport="telegram",
        transport_message_id=203,
        requester_identity="telegram:12345",
        delivery_target=12345,
        request_mode="literal",
        source_text="Test source text for auth check.",
        hold_for_approval=True,
    )
    res = process_herald_request(db_session, req)
    job_id = res.job_id

    mock_client = MagicMock(spec=TelegramClient)

    # 1. Intruder user ID
    cb_intruder = {
        "id": "cb-int",
        "from": {"id": 99999},
        "message": {"message_id": 888, "chat": {"id": 99999, "type": "private"}},
        "data": f"h2:approve:{job_id}",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_intruder)
    mock_client.answer_callback_query.assert_called_with("cb-int", text="Unauthorized: Access denied.", show_alert=True)

    job = db_session.query(PodcastJob).filter_by(id=job_id).first()
    assert job.status == JobState.AWAITING_APPROVAL.value  # Unchanged
