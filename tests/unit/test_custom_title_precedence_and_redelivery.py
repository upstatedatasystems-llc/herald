from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.core.models import HeraldRequest
from herald.core.pipeline import process_herald_request
from herald.db.models import Base, JobState, PodcastJob
from herald.telegram.auth import generate_pairing_code, verify_and_claim_pairing_code
from herald.telegram.bot import handle_telegram_content_message
from herald.telegram.client import TelegramClient


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


def test_custom_title_precedence_in_herald_response_and_duplicates(db_session, monkeypatch):
    """
    Test that explicit custom_title is authoritative across:
    - Newly created/scripted HeraldResponse
    - Duplicate by Telegram message
    - Duplicate by source content & settings
    """
    monkeypatch.setattr("herald.core.pipeline.settings.AI_PROVIDER", "gemini")
    monkeypatch.setattr("herald.core.pipeline.settings.GEMINI_API_KEY", "dummy_key")

    mock_provider = MagicMock()
    mock_provider.model_name = "gemini-3.5-flash"
    mock_provider.generate_script.return_value = MagicMock(
        model_dump=lambda: {
            "episode_title": "AI Generated Episode Title",
            "episode_description": "AI Generated Description",
            "segments": [{"speaker": "Host", "narration": "Hello world from AI script."}],
        }
    )

    with patch("herald.core.pipeline.get_ai_provider", return_value=mock_provider):
        # 1. Newly created job with custom_title
        req1 = HeraldRequest(
            requester_identity="telegram:12345",
            transport="telegram",
            delivery_target="12345",
            transport_message_id="101",
            source_text="Sample podcast source text for test",
            custom_title="Explicit User Title",
            request_mode="standard",
        )
        resp1 = process_herald_request(db_session, req1)
        assert resp1.is_duplicate is False
        assert resp1.episode_title == "Explicit User Title"  # Explicit title wins over AI script!

        # 2. Duplicate by Telegram message
        req_dup_msg = HeraldRequest(
            requester_identity="telegram:12345",
            transport="telegram",
            delivery_target="12345",
            transport_message_id="101",
            source_text="Sample podcast source text for test",
            custom_title="Explicit User Title",
            request_mode="standard",
        )
        resp_dup_msg = process_herald_request(db_session, req_dup_msg)
        assert resp_dup_msg.is_duplicate is True
        assert resp_dup_msg.episode_title == "Explicit User Title"

        # 3. Duplicate by source hash / content (different message_id)
        req_dup_content = HeraldRequest(
            requester_identity="telegram:12345",
            transport="telegram",
            delivery_target="12345",
            transport_message_id="102",
            source_text="Sample podcast source text for test",
            custom_title="Explicit User Title",
            request_mode="standard",
        )
        resp_dup_content = process_herald_request(db_session, req_dup_content)
        assert resp_dup_content.is_duplicate is True
        assert resp_dup_content.episode_title == "Explicit User Title"


def test_duplicate_telegram_redelivery_uses_html_parse_mode_and_display_title(db_session, tmp_path):
    """
    Test that duplicate re-delivery of completed job:
    1. Sends notification and send_audio
    2. Passes parse_mode="HTML" to send_audio
    3. Uses authoritative display title in caption and title metadata
    """
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    local_audio = tmp_path / "completed_ep.mp3"
    local_audio.write_bytes(b"dummy_mp3_data")

    job = PodcastJob(
        id="redeliver-test-job-1111-2222-333333333333",
        transport="telegram",
        telegram_user_id=12345,
        telegram_chat_id=12345,
        telegram_message_id=50,
        status=JobState.COMPLETE.value,
        source_hash="h_redeliver",
        source_text="Duplicate test content",
        custom_title="Tom & Jerry <Special>",
        local_audio_path=str(local_audio),
        script_json={"episode_title": "AI Title Should Be Ignored"},
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)

    # User re-sends identical message
    msg = {
        "chat": {"id": 12345, "type": "private"},
        "from": {"id": 12345},
        "message_id": 50,  # Same transport_message_id
        "text": "Duplicate test content",
    }

    handle_telegram_content_message(db_session, mock_client, msg)

    # Verify send_audio was called with HTML parse_mode and escaped title
    mock_client.send_audio.assert_called_once()
    audio_kwargs = mock_client.send_audio.call_args[1]
    assert audio_kwargs["parse_mode"] == "HTML"
    assert audio_kwargs["title"] == "Tom & Jerry <Special>"
    assert "Tom &amp; Jerry &lt;Special&gt;" in audio_kwargs["caption"]
    assert "<Special>" not in audio_kwargs["caption"]
