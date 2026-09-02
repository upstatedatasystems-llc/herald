import os
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from herald.db.models import JobState, PodcastJob
from herald.telegram.client import TelegramClient
from herald.telegram.delivery import deliver_pending_telegram_jobs, deliver_single_job


def test_completed_mp3_delivered_to_telegram(db_session, tmp_path):
    """Point 18: Completed MP3 is sent through Telegram."""
    audio_file = tmp_path / "test_ep.mp3"
    audio_file.write_bytes(b"\xFF\xFB\x90\x44" * 100)  # fake MP3 header bytes

    job = PodcastJob(
        id="tg-job-001",
        transport="telegram",
        telegram_chat_id=778899,
        telegram_message_id=101,
        status=JobState.AUDIO_READY.value,
        request_mode="literal",
        source_hash="hash001",
        source_text="Test source",
        local_audio_path=str(audio_file),
        audio_duration_seconds=125,
        script_json={"episode_title": "AI in Healthcare"},
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.is_configured = True

    count = deliver_pending_telegram_jobs(db_session, mock_client)
    assert count == 1

    mock_client.send_audio.assert_called_once()
    call_args = mock_client.send_audio.call_args[1]
    assert call_args["chat_id"] == 778899
    assert call_args["title"] == "AI in Healthcare"
    assert call_args["duration"] == 125
    assert "2m 5s" in call_args["caption"]

    db_session.refresh(job)
    assert job.status == JobState.COMPLETE.value
    assert job.delivered_at is not None


def test_delivery_retry_reuses_existing_mp3(db_session, tmp_path):
    """Point 19: Delivery retry reuses existing MP3 without re-synthesis."""
    audio_file = tmp_path / "persisted_ep.mp3"
    audio_file.write_bytes(b"\xFF\xFB\x90\x44" * 200)

    job = PodcastJob(
        id="tg-job-retry-002",
        transport="telegram",
        telegram_chat_id=778899,
        telegram_message_id=102,
        status=JobState.AUDIO_READY.value,
        request_mode="literal",
        source_hash="hash002",
        source_text="Test source",
        local_audio_path=str(audio_file),
        audio_duration_seconds=60,
        script_json={"episode_title": "Retry Episode"},
        delivery_attempt_count=1,
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.is_configured = True

    deliver_single_job(db_session, job, mock_client)

    # Audio was read directly from existing local_audio_path without re-running TTS
    mock_client.send_audio.assert_called_once()
    assert Path(mock_client.send_audio.call_args[1]["audio_path"]) == audio_file

    db_session.refresh(job)
    assert job.status == JobState.COMPLETE.value
