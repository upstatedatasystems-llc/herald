from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.db.models import Base, JobState, PodcastJob
from herald.services.settings_fingerprint import build_generation_settings_snapshot
from herald.telegram.bot import handle_telegram_callback_query
from herald.telegram.client import TelegramClient
from herald.telegram.formatters import format_approval, format_rerun_confirmation


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_format_approval_with_prior_job_identical_settings():
    now = datetime.now(UTC)
    snap = build_generation_settings_snapshot("standard", voice="af_heart", speed=1.0)

    prior = PodcastJob(
        id="prior-1111-2222",
        status=JobState.COMPLETE.value,
        request_mode="standard",
        custom_voice="af_heart",
        custom_speed=1.0,
        generation_settings_json=snap,
        created_at=now,
    )
    current = PodcastJob(
        id="curr-3333-4444",
        status=JobState.AWAITING_APPROVAL.value,
        request_mode="standard",
        custom_voice="af_heart",
        custom_speed=1.0,
        generation_settings_json=snap,
        source_text="Sample content",
        created_at=now,
    )

    text, markup = format_approval(current, script_json={"episode_title": "Test Ep"}, prior_job=prior)
    assert "Podcast Rerun Ready for Approval" in text
    assert "Prior Generation:" in text
    assert "prior-11" in text
    assert "Identical to prior run" in text
    assert markup["inline_keyboard"][0][0]["text"] == "✅ Approve Rerun & Generate"
    assert markup["inline_keyboard"][0][0]["callback_data"] == f"h2:approve:{current.id}"


def test_format_approval_with_prior_job_modified_settings():
    now = datetime.now(UTC)
    snap_prior = build_generation_settings_snapshot("brief", voice="af_heart", speed=1.0)
    snap_curr = build_generation_settings_snapshot("standard", voice="af_bella", speed=1.1)

    prior = PodcastJob(
        id="prior-aaaa-bbbb",
        status=JobState.COMPLETE.value,
        generation_settings_json=snap_prior,
        created_at=now,
    )
    current = PodcastJob(
        id="curr-cccc-dddd",
        status=JobState.AWAITING_APPROVAL.value,
        generation_settings_json=snap_curr,
        source_text="Sample content",
        created_at=now,
    )

    text, markup = format_rerun_confirmation(new_job=current, prior_job=prior)
    assert "Prior Generation Found" in text
    assert "prior-aa" in text
    assert "Settings Modified:" in text
    assert markup["inline_keyboard"][0][0]["text"] == "🔄 Confirm Rerun"
    assert markup["inline_keyboard"][0][0]["callback_data"] == f"h2:rerun_approve:{current.id}"
    assert markup["inline_keyboard"][0][1]["text"] == "❌ Cancel"
    assert markup["inline_keyboard"][0][1]["callback_data"] == f"h2:deny:{current.id}"


def test_callback_rerun_approve_success(db_session):
    now = datetime.now(UTC)
    snap = build_generation_settings_snapshot("literal", voice="af_heart", speed=1.0)

    job = PodcastJob(
        id="job-rerun-test-1",
        transport="telegram",
        telegram_chat_id=12345,
        telegram_user_id=67890,
        request_mode="literal",
        source_hash="test-hash-callback-1",
        source_text="This is test text for callback rerun verification.",
        status=JobState.AWAITING_RERUN_CONFIRMATION.value,
        generation_settings_json=snap,
        created_at=now,
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    cb_query = {
        "id": "cb-100",
        "data": f"h2:rerun_approve:{job.id}",
        "from": {"id": 67890},
        "message": {"message_id": 999, "chat": {"id": 12345, "type": "private"}},
    }

    with patch("herald.telegram.bot.is_user_authorized", return_value=True):
        handle_telegram_callback_query(db_session, mock_client, cb_query)

    db_session.refresh(job)
    assert job.status == JobState.QUEUED_TTS.value
    assert job.script_json is not None
    mock_client.answer_callback_query.assert_any_call("cb-100", text="Rerun confirmed! Generating script...")


def test_callback_deny_cancels_awaiting_rerun_confirmation(db_session):
    now = datetime.now(UTC)
    job = PodcastJob(
        id="job-deny-rerun-test",
        transport="telegram",
        telegram_chat_id=12345,
        telegram_user_id=67890,
        request_mode="literal",
        source_hash="test-hash-callback-2",
        source_text="Sample text",
        status=JobState.AWAITING_RERUN_CONFIRMATION.value,
        created_at=now,
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    cb_query = {
        "id": "cb-200",
        "data": f"h2:deny:{job.id}",
        "from": {"id": 67890},
        "message": {"message_id": 999, "chat": {"id": 12345, "type": "private"}},
    }

    with patch("herald.telegram.bot.is_user_authorized", return_value=True):
        handle_telegram_callback_query(db_session, mock_client, cb_query)

    db_session.refresh(job)
    assert job.status == JobState.CANCELLED.value
    mock_client.answer_callback_query.assert_called_with("cb-200", text="Generation cancelled.")
