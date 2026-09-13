from unittest.mock import MagicMock, patch
import pytest

from herald.db.models import JobState, PodcastJob, TelegramUser
from herald.telegram.auth import (
    generate_pairing_code,
    get_effective_user_preferences,
    verify_and_claim_pairing_code,
)
from herald.telegram.bot import (
    handle_telegram_callback_query,
    handle_telegram_content_message,
)
from herald.telegram.client import TelegramClient


@pytest.fixture
def authorized_user(db_session):
    code = generate_pairing_code(db_session)
    user = verify_and_claim_pairing_code(
        db_session, code, user_id=99999, chat_id=99999, username="test_owner"
    )
    return user


def test_input_classification_url_and_text(db_session, authorized_user, monkeypatch):
    """Test conservative input classification: URL, long text (>300 chars), short text (<=300 chars), directive."""
    mock_client = MagicMock(spec=TelegramClient)
    mock_client.send_message.return_value = {"message_id": 888}

    # 1. Short text (<= 300 chars) -> defaults to Topic mode
    msg_short = {
        "message_id": 1,
        "from": {"id": 99999},
        "chat": {"id": 99999, "type": "private"},
        "text": "Future of fusion energy reactors and grid integration.",
    }
    handle_telegram_content_message(db_session, mock_client, msg_short)
    mock_client.send_message.assert_called_once()
    job_short = db_session.query(PodcastJob).filter(PodcastJob.telegram_message_id == 1).first()
    assert job_short is not None
    assert job_short.status == JobState.AWAITING_CONFIGURATION.value
    assert job_short.content_mode == "topic"
    assert job_short.telegram_config_message_id == 888

    # 2. Long text (> 300 chars) -> defaults to Source mode
    mock_client.reset_mock()
    long_content = (
        "In a landmark development for renewable infrastructure, researchers have announced a new "
        "generation of solid-state grid storage batteries. These systems demonstrate a tenfold increase "
        "in cycle longevity compared to standard lithium-ion chemistries. Production prototypes are "
        "scheduled for commercial grid testing across European distribution networks starting next quarter."
    )
    assert len(long_content) > 300
    msg_long = {
        "message_id": 2,
        "from": {"id": 99999},
        "chat": {"id": 99999, "type": "private"},
        "text": long_content,
    }
    handle_telegram_content_message(db_session, mock_client, msg_long)
    mock_client.send_message.assert_called_once()
    job_long = db_session.query(PodcastJob).filter(PodcastJob.telegram_message_id == 2).first()
    assert job_long is not None
    assert job_long.status == JobState.AWAITING_CONFIGURATION.value
    assert job_long.content_mode == "source"

    # 3. Explicit directive: "literal" -> initializes in Literal mode
    mock_client.reset_mock()
    msg_directive = {
        "message_id": 3,
        "from": {"id": 99999},
        "chat": {"id": 99999, "type": "private"},
        "text": "literal\nPlease read this verbatim without AI interpretation.",
    }
    handle_telegram_content_message(db_session, mock_client, msg_directive)
    job_dir = db_session.query(PodcastJob).filter(PodcastJob.telegram_message_id == 3).first()
    assert job_dir is not None
    assert job_dir.status == JobState.AWAITING_CONFIGURATION.value
    assert job_dir.content_mode == "literal"


def test_config_card_callbacks(db_session, authorized_user):
    """Test interactive config card buttons: mode, length, research depth, defaults, and cancel."""
    mock_client = MagicMock(spec=TelegramClient)

    job = PodcastJob(
        id="c0000000-0000-0000-0000-000000000001",
        transport="telegram",
        telegram_user_id=99999,
        telegram_chat_id=99999,
        telegram_message_id=10,
        telegram_config_message_id=20,
        source_type="pasted_text",
        source_hash="test-hash-123",
        source_text="Test source text for callback checks.",
        content_mode="source",
        target_minutes="auto",
        research_depth="medium",
        status=JobState.AWAITING_CONFIGURATION.value,
    )
    db_session.add(job)
    db_session.commit()

    # 1. Switch mode to 'expanded'
    cb_mode = {
        "id": "cb-1",
        "from": {"id": 99999},
        "message": {"message_id": 20, "chat": {"id": 99999, "type": "private"}},
        "data": f"h4:c:{job.id}:m:expanded",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_mode)
    db_session.refresh(job)
    assert job.content_mode == "expanded"
    mock_client.edit_message_text.assert_called()

    # 2. Switch length to '30' min
    mock_client.reset_mock()
    cb_len = {
        "id": "cb-2",
        "from": {"id": 99999},
        "message": {"message_id": 20, "chat": {"id": 99999, "type": "private"}},
        "data": f"h4:c:{job.id}:len:30",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_len)
    db_session.refresh(job)
    assert job.target_minutes == "30"

    # 3. Switch research depth to 'high'
    mock_client.reset_mock()
    cb_rd = {
        "id": "cb-3",
        "from": {"id": 99999},
        "message": {"message_id": 20, "chat": {"id": 99999, "type": "private"}},
        "data": f"h4:c:{job.id}:rd:high",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_rd)
    db_session.refresh(job)
    assert job.research_depth == "high"

    # 4. Cancel podcast
    mock_client.reset_mock()
    cb_cancel = {
        "id": "cb-5",
        "from": {"id": 99999},
        "message": {"message_id": 20, "chat": {"id": 99999, "type": "private"}},
        "data": f"h4:c:{job.id}:btn:cancel",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_cancel)
    db_session.refresh(job)
    assert job.status == JobState.CANCELLED.value

    # 5. Shortcut: Literal mode (immediate start on separate job)
    job_lit = PodcastJob(
        id="c0000000-0000-0000-0000-000000000009",
        transport="telegram",
        telegram_user_id=99999,
        telegram_chat_id=99999,
        telegram_message_id=12,
        telegram_config_message_id=29,
        source_type="pasted_text",
        source_hash="test-hash-lit-shortcut",
        source_text="Test source text for callback checks.",
        content_mode="source",
        target_minutes="auto",
        research_depth="medium",
        status=JobState.AWAITING_CONFIGURATION.value,
    )
    db_session.add(job_lit)
    db_session.commit()

    mock_client.reset_mock()
    cb_lit = {
        "id": "cb-4",
        "from": {"id": 99999},
        "message": {"message_id": 20, "chat": {"id": 99999, "type": "private"}},
        "data": f"h4:c:{job_lit.id}:btn:lit",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_lit)
    db_session.refresh(job_lit)
    assert job_lit.content_mode == "literal"
    assert job_lit.status == JobState.SCRIPTING.value


def test_settings_defaults_callbacks(db_session, authorized_user):
    """Test updating default mode, length, and research depth via settings callbacks."""
    mock_client = MagicMock(spec=TelegramClient)

    # 1. Update default content mode
    cb_mode = {
        "id": "cb-s1",
        "from": {"id": 99999},
        "message": {"message_id": 30, "chat": {"id": 99999, "type": "private"}},
        "data": "h4:s:set_mode:expanded",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_mode)
    prefs = get_effective_user_preferences(db_session, 99999)
    assert prefs["default_content_mode"] == "expanded"

    # 2. Update default length
    cb_len = {
        "id": "cb-s2",
        "from": {"id": 99999},
        "message": {"message_id": 30, "chat": {"id": 99999, "type": "private"}},
        "data": "h4:s:set_len:45",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_len)
    prefs = get_effective_user_preferences(db_session, 99999)
    assert prefs["default_target_minutes"] == "45"

    # 3. Update default research depth
    cb_rd = {
        "id": "cb-s3",
        "from": {"id": 99999},
        "message": {"message_id": 30, "chat": {"id": 99999, "type": "private"}},
        "data": "h4:s:set_rd:high",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_rd)
    prefs = get_effective_user_preferences(db_session, 99999)
    assert prefs["default_research_depth"] == "high"


def test_config_card_start_generation(db_session, authorized_user):
    """Test clicking 'Create Podcast' on the card triggers script generation and transitions."""
    mock_client = MagicMock(spec=TelegramClient)

    # Test Literal mode execution (zero AI calls)
    job = PodcastJob(
        id="c0000000-0000-0000-0000-000000000002",
        transport="telegram",
        telegram_user_id=99999,
        telegram_chat_id=99999,
        telegram_message_id=11,
        telegram_config_message_id=21,
        source_type="pasted_text",
        source_hash="test-hash-start-1",
        source_text="This is a test article for the start button.\n\nSecond paragraph for reading.",
        content_mode="literal",
        target_minutes="auto",
        research_depth="none",
        status=JobState.AWAITING_CONFIGURATION.value,
    )
    db_session.add(job)
    db_session.commit()

    cb_start = {
        "id": "cb-start-1",
        "from": {"id": 99999},
        "message": {"message_id": 21, "chat": {"id": 99999, "type": "private"}},
        "data": f"h4:c:{job.id}:btn:start",
    }
    handle_telegram_callback_query(db_session, mock_client, cb_start)
    db_session.refresh(job)

    # Fast Telegram acknowledgment queues job for worker
    assert job.status == JobState.SCRIPTING.value

    # Worker executes script generation asynchronously
    from apps.worker.main import process_next_scripting_job

    success = process_next_scripting_job(db_session, worker_id="test-worker")
    assert success is True

    db_session.refresh(job)
    # Should have generated script and queued for TTS (confirm_before_tts is False by default)
    assert job.status == JobState.QUEUED_TTS.value
    assert job.script_json is not None
    assert len(job.script_json.get("segments", [])) > 0

