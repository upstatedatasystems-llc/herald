from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.db.models import Base, JobState, PodcastJob
from herald.telegram.client import TelegramClient
from herald.telegram.delivery import deliver_single_job
from herald.telegram.formatters import (
    format_completion,
    format_queued,
    get_job_display_title,
)


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


def test_telegram_client_send_audio_and_document_parse_mode_payloads(tmp_path):
    """
    TelegramClient.send_audio and send_document pass parse_mode in:
    - JSON payload (file_id)
    - Multipart form payload (local file)
    """
    client = TelegramClient(token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    dummy_file = tmp_path / "test.mp3"
    dummy_file.write_bytes(b"dummy_bytes")

    with patch("httpx.Client.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True, "result": {"message_id": 100}}
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        # 1. send_audio with file_id (JSON payload)
        client.send_audio(
            chat_id=123,
            file_id="tg_audio_file_id_123",
            caption="<b>Rich Caption</b>",
            parse_mode="HTML",
        )
        assert mock_post.called
        json_payload = mock_post.call_args[1]["json"]
        assert json_payload["parse_mode"] == "HTML"
        assert json_payload["caption"] == "<b>Rich Caption</b>"

        # 2. send_audio with local file (Multipart payload)
        mock_post.reset_mock()
        client.send_audio(
            chat_id=123,
            audio_path=dummy_file,
            caption="<b>Rich Caption</b>",
            parse_mode="HTML",
        )
        assert mock_post.called
        data_payload = mock_post.call_args[1]["data"]
        assert data_payload["parse_mode"] == "HTML"

        # 3. send_document with file_id (JSON payload)
        mock_post.reset_mock()
        client.send_document(
            chat_id=123,
            file_id="tg_doc_file_id_123",
            caption="<i>Document Caption</i>",
            parse_mode="HTML",
        )
        assert mock_post.called
        json_doc_payload = mock_post.call_args[1]["json"]
        assert json_doc_payload["parse_mode"] == "HTML"

        # 4. send_document with local file (Multipart payload)
        mock_post.reset_mock()
        client.send_document(
            chat_id=123,
            document_path=dummy_file,
            caption="<i>Document Caption</i>",
            parse_mode="HTML",
        )
        assert mock_post.called
        data_doc_payload = mock_post.call_args[1]["data"]
        assert data_doc_payload["parse_mode"] == "HTML"


def test_title_override_precedence_everywhere():
    """
    Job display title precedence:
    job.custom_title -> (job.script_json or {}).get("episode_title") -> "Herald Episode"
    An explicit custom_title always wins over AI generated script title.
    """
    job = PodcastJob(
        id="title-precedence-job-1",
        source_text="Sample text",
        custom_title="Authoritative Explicit Title",
        script_json={"episode_title": "AI Generated Episode Title"},
    )
    assert get_job_display_title(job) == "Authoritative Explicit Title"

    job_no_custom = PodcastJob(
        id="title-precedence-job-2",
        source_text="Sample text",
        custom_title=None,
        script_json={"episode_title": "AI Generated Episode Title"},
    )
    assert get_job_display_title(job_no_custom) == "AI Generated Episode Title"

    job_default = PodcastJob(
        id="title-precedence-job-3",
        source_text="Sample text",
        custom_title="",
        script_json={},
    )
    assert get_job_display_title(job_default) == "Herald Episode"


def test_oversized_audio_escapes_dynamic_title_html(db_session, tmp_path):
    """
    Oversized audio warning properly escapes HTML in episode titles such as 'Tom & Jerry <Episode>'.
    """
    large_file = tmp_path / "large.mp3"
    large_file.write_bytes(b"0" * 1000)

    job = PodcastJob(
        id="oversized-test-job-1",
        transport="telegram",
        telegram_user_id=123,
        telegram_chat_id=123,
        status=JobState.AUDIO_READY.value,
        custom_title="Tom & Jerry <Episode>",
        local_audio_path=str(large_file),
        source_hash="h_over",
        source_text="Test source text",
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)

    with patch("herald.telegram.delivery.settings.TELEGRAM_MAX_AUDIO_BYTES", 500):
        res = deliver_single_job(db_session, job, mock_client)

    assert res is False
    mock_client.send_message.assert_called_once()
    sent_text = mock_client.send_message.call_args[1]["text"]
    assert "Tom &amp; Jerry &lt;Episode&gt;" in sent_text
    assert "<Episode>" not in sent_text


def test_format_queued_and_completion_rich_metadata():
    """
    Test rich queued and completion formatters:
    - Queued includes source info and truthful capacity wording
    - Completion includes source and narration word counts, respects 1024-char limit
    """
    job = PodcastJob(
        id="rich-metadata-job-12345678",
        transport="telegram",
        request_mode="standard",
        source_type="url",
        source_url="https://example.com/very/long/article/path",
        source_text="This is a test source text with ten words in total here.",
        script_json={
            "episode_title": "Rich Metadata Test Episode",
            "episode_description": "A deep dive into everything that happened today.",
            "segments": [
                {"narration": "Welcome to Herald podcast narration text."},
                {"narration": "Here is the second segment of narration."},
            ],
        },
        audio_duration_seconds=185,
    )

    # Queued card
    queued_msg = format_queued(job, job.script_json)
    assert "URL (https://example.com/very/long/artic...)" in queued_msg
    assert "Queued for synthesis. Herald will begin when TTS capacity is available." in queued_msg
    assert "Synthesizing audio now." not in queued_msg

    # Completion card
    comp_msg = format_completion(
        job=job,
        actual_chunks_count=3,
        file_size_bytes=4 * 1024 * 1024,
        active_processing_seconds=42,
    )
    assert "• <b>Words:</b> 12 src / 13 nar" in comp_msg
    assert "• <b>Processing Time:</b> 42s" in comp_msg
    assert "• <b>TTS Chunks:</b> 3 chunks" in comp_msg
    assert len(comp_msg) <= 1024


def test_first_delivery_caption_includes_active_processing_time(db_session, tmp_path):
    """
    deliver_single_job calculates active processing time as-of delivery timestamp and
    includes it in the very first sendAudio caption.
    """
    now = datetime.now(UTC)
    audio_file = tmp_path / "ep_delivery.mp3"
    audio_file.write_bytes(b"dummy_mp3_data")

    job = PodcastJob(
        id="proc-time-delivery-job-1",
        transport="telegram",
        telegram_user_id=123,
        telegram_chat_id=123,
        status=JobState.AUDIO_READY.value,
        custom_title="Processing Time Test",
        local_audio_path=str(audio_file),
        source_hash="h_pt",
        source_text="Test source text for processing time",
        created_at=now - timedelta(seconds=120),
        approval_requested_at=now - timedelta(seconds=100),
        approved_at=now
        - timedelta(seconds=40),  # Held for approval 60s -> Active time = 120 - 60 = 60s
        audio_duration_seconds=75,
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.send_audio.return_value = {"message_id": 201, "audio": {"file_id": "aud_123"}}

    res = deliver_single_job(db_session, job, mock_client)
    assert res is True

    mock_client.send_audio.assert_called_once()
    caption = mock_client.send_audio.call_args[1]["caption"]
    assert (
        "• <b>Processing Time:</b> 1m 0s" in caption
        or "• <b>Processing Time:</b> 1m" in caption
        or "• <b>Processing Time:</b> 60s" in caption
    )
