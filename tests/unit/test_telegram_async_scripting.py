"""Unit tests for Asynchronous Scripting, Telegram Shortcuts, Worker Claiming, and Recovery.
Tests:
- Telegram callbacks (btn:start, btn:def, btn:lit) transition to SCRIPTING immediately and return without blocking
- Worker claims SCRIPTING job atomically
- Stale SCRIPTING claim recovery without failing human wait states
- Recovery of unpresented AWAITING_CONFIGURATION cards
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.worker.main import claim_next_scripting_job, recover_stale_claims
from herald.db.models import Base, JobState, PodcastJob, TelegramUser
from herald.telegram.approval_recovery import sweep_unpresented_approval_cards
from herald.telegram.bot import handle_telegram_callback_query


@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_telegram_callback_btn_start_transitions_to_scripting(test_db):
    user = TelegramUser(
        id="u-1",
        telegram_user_id=1001,
        telegram_chat_id=1001,
        role="owner",
        is_active=True,
    )
    job = PodcastJob(
        id="job-async-1",
        source_hash="hash-async-1",
        source_text="Sample source text",
        telegram_user_id=1001,
        telegram_chat_id=1001,
        status=JobState.AWAITING_CONFIGURATION.value,
        content_mode="source",
        target_minutes="auto",
        telegram_config_message_id=555,
    )
    test_db.add_all([user, job])
    test_db.commit()

    mock_client = MagicMock()
    mock_client.answer_callback_query.return_value = True
    mock_client.edit_message_text.return_value = True

    cb_query = {
        "id": "cb-123",
        "data": f"h4:c:{job.id}:btn:start",
        "from": {"id": 1001},
        "message": {
            "message_id": 555,
            "chat": {"id": 1001, "type": "private"},
        },
    }

    handle_telegram_callback_query(test_db, mock_client, cb_query)

    test_db.refresh(job)
    # Status transitioned to SCRIPTING and unclaimed
    assert job.status == JobState.SCRIPTING.value
    assert job.claimed_by is None
    assert job.claim_owner is None
    # Telegram feedback sent
    mock_client.answer_callback_query.assert_called_once()
    mock_client.edit_message_text.assert_called_once()
    call_kwargs = mock_client.edit_message_text.call_args[1]
    assert "Queued for script generation" in call_kwargs["text"]


def test_telegram_callback_btn_def_and_btn_lit_shortcuts(test_db):
    user = TelegramUser(
        id="u-2",
        telegram_user_id=1002,
        telegram_chat_id=1002,
        role="owner",
        is_active=True,
        default_content_mode="expanded",
        default_target_minutes="30",
        default_research_depth="high",
    )
    job_def = PodcastJob(
        id="job-def-1",
        source_hash="hash-def-1",
        source_text="Sample source text",
        telegram_user_id=1002,
        telegram_chat_id=1002,
        status=JobState.AWAITING_CONFIGURATION.value,
        telegram_config_message_id=556,
    )
    test_db.add_all([user, job_def])
    test_db.commit()

    mock_client = MagicMock()

    # Test btn:def applies saved defaults and transitions immediately to SCRIPTING
    cb_query_def = {
        "id": "cb-def",
        "data": f"h4:c:{job_def.id}:btn:def",
        "from": {"id": 1002},
        "message": {"message_id": 556, "chat": {"id": 1002, "type": "private"}},
    }
    handle_telegram_callback_query(test_db, mock_client, cb_query_def)
    test_db.refresh(job_def)
    assert job_def.status == JobState.SCRIPTING.value
    assert job_def.content_mode == "expanded"
    assert job_def.target_minutes == "30"
    assert job_def.research_depth == "high"
    assert job_def.resolved_default is True

    # Test btn:lit sets literal mode and transitions immediately to SCRIPTING
    job_lit = PodcastJob(
        id="job-lit-1",
        source_hash="hash-lit-1",
        source_text="Sample source text",
        telegram_user_id=1002,
        telegram_chat_id=1002,
        status=JobState.AWAITING_CONFIGURATION.value,
        telegram_config_message_id=557,
    )
    test_db.add(job_lit)
    test_db.commit()

    cb_query_lit = {
        "id": "cb-lit",
        "data": f"h4:c:{job_lit.id}:btn:lit",
        "from": {"id": 1002},
        "message": {"message_id": 557, "chat": {"id": 1002, "type": "private"}},
    }
    handle_telegram_callback_query(test_db, mock_client, cb_query_lit)
    test_db.refresh(job_lit)
    assert job_lit.status == JobState.SCRIPTING.value
    assert job_lit.content_mode == "literal"
    assert job_lit.target_minutes == "auto"
    assert job_lit.research_depth is None


def test_worker_claim_scripting_job(test_db):
    job = PodcastJob(
        id="job-scripting-claim",
        source_hash="hash-scripting-claim",
        source_text="Sample source text",
        status=JobState.SCRIPTING.value,
        content_mode="source",
        target_minutes="auto",
    )
    test_db.add(job)
    test_db.commit()

    claimed = claim_next_scripting_job(test_db, worker_id="test-worker-1", lease_seconds=300)
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.claimed_by == "test-worker-1"
    assert claimed.claim_owner == "test-worker-1"
    assert claimed.lease_expires_at is not None

    # Second worker cannot claim the already-claimed job
    claimed_again = claim_next_scripting_job(test_db, worker_id="test-worker-2", lease_seconds=300)
    assert claimed_again is None


def test_recover_stale_scripting_claims_preserves_human_wait_states(test_db):
    now = datetime.now(UTC)
    old_time = now - timedelta(minutes=30)

    # 1. Stale SCRIPTING job: should have claim cleared and remain SCRIPTING for retry
    job_scripting = PodcastJob(
        id="job-stale-scripting",
        source_hash="hash-stale-scripting",
        source_text="Sample source text",
        status=JobState.SCRIPTING.value,
        claimed_by="dead-worker",
        claim_owner="dead-worker",
        claimed_at=old_time,
        heartbeat_at=old_time,
        lease_expires_at=old_time + timedelta(seconds=300),
    )

    # 2. Human wait state: AWAITING_CONFIGURATION (must NOT be touched or failed)
    job_await_cfg = PodcastJob(
        id="job-await-cfg",
        source_hash="hash-await-cfg",
        source_text="Sample source text",
        status=JobState.AWAITING_CONFIGURATION.value,
        created_at=old_time,
    )

    # 3. Human wait state: AWAITING_APPROVAL (must NOT be touched or failed)
    job_await_app = PodcastJob(
        id="job-await-app",
        source_hash="hash-await-app",
        source_text="Sample source text",
        status=JobState.AWAITING_APPROVAL.value,
        created_at=old_time,
    )

    test_db.add_all([job_scripting, job_await_cfg, job_await_app])
    test_db.commit()

    recover_stale_claims(test_db, stale_minutes=15)

    test_db.refresh(job_scripting)
    test_db.refresh(job_await_cfg)
    test_db.refresh(job_await_app)

    # Stale scripting lease recovered cleanly without failing job
    assert job_scripting.status == JobState.SCRIPTING.value
    assert job_scripting.claimed_by is None
    assert job_scripting.claim_owner is None
    assert job_scripting.lease_expires_at is None

    # Human wait states untouched
    assert job_await_cfg.status == JobState.AWAITING_CONFIGURATION.value
    assert job_await_app.status == JobState.AWAITING_APPROVAL.value


def test_recover_unpresented_config_cards(test_db):
    job = PodcastJob(
        id="job-unpresented-cfg",
        source_hash="hash-unpresented-cfg",
        transport="telegram",
        status=JobState.AWAITING_CONFIGURATION.value,
        telegram_chat_id=9999,
        telegram_user_id=9999,
        telegram_message_id="123",
        telegram_config_message_id=None,  # Not delivered yet
        source_text="Test source text.",
        attempt_count=0,
    )
    test_db.add(job)
    test_db.commit()

    mock_client = MagicMock()
    mock_client.send_message.return_value = {"message_id": 8888}

    delivered = sweep_unpresented_approval_cards(test_db, mock_client)

    test_db.refresh(job)
    assert delivered >= 1
    assert job.telegram_config_message_id == 8888
    mock_client.send_message.assert_called_once()
