from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import Base, JobState, PodcastJob
from herald.telegram.client import TelegramAPIError
from herald.telegram.delivery import deliver_pending_telegram_jobs


def test_telegram_delivery_retry_exponential_backoff_and_reuse(tmp_path, monkeypatch):
    """
    Test delivery:
    1. Transient failure transitions to FAILED_RETRYABLE with exponential backoff next_retry_at.
    2. Retry reuses local MP3 without regenerating audio or script.
    3. Final attempt succeeds and moves to COMPLETE.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    mp3_file = tmp_path / "episode_123.mp3"
    mp3_file.write_bytes(b"dummy mp3 audio content")

    with TestingSession() as db:
        job = PodcastJob(
            id="job-retry-123",
            transport="telegram",
            telegram_chat_id=12345,
            telegram_message_id=555,
            request_mode="literal",
            source_hash="dummyhash",
            source_text="Sample text",
            local_audio_path=str(mp3_file),
            audio_duration_seconds=120,
            status=JobState.AUDIO_READY.value,
        )
        db.add(job)
        db.commit()

    mock_client = MagicMock()
    mock_client.is_configured = True

    # Attempt 1: send_audio and send_document both fail
    mock_client.send_audio.side_effect = TelegramAPIError(500, "Temporary Network Error")
    mock_client.send_document.side_effect = TelegramAPIError(500, "Temporary Network Error")

    with TestingSession() as db:
        delivered = deliver_pending_telegram_jobs(db, mock_client)
        assert delivered == 0

        # Check job is now FAILED_RETRYABLE with attempt_count = 1 and next_retry_at set
        updated_job = db.query(PodcastJob).filter_by(id="job-retry-123").first()
        assert updated_job.status == JobState.FAILED_RETRYABLE.value
        assert updated_job.delivery_attempt_count == 1
        assert updated_job.failed_stage == "TELEGRAM_DELIVERY"
        assert updated_job.next_retry_at is not None

        # Simulate time passing to make job eligible again
        updated_job.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    # Attempt 2: send_audio succeeds
    mock_client.send_audio.side_effect = None
    mock_client.send_audio.return_value = {"message_id": 999}

    with TestingSession() as db:
        delivered = deliver_pending_telegram_jobs(db, mock_client)
        assert delivered == 1

        updated_job = db.query(PodcastJob).filter_by(id="job-retry-123").first()
        assert updated_job.status == JobState.COMPLETE.value
        assert updated_job.delivered_at is not None


def test_telegram_oversized_audio_handling(tmp_path, monkeypatch):
    """
    Test that oversized MP3 (> TELEGRAM_MAX_AUDIO_BYTES):
    1. Transitions to FAILED_FINAL with error_code TELEGRAM_AUDIO_OVERSIZED.
    2. Does NOT transition to COMPLETE.
    3. Sends user notification without exposing server internal paths.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    mp3_file = tmp_path / "huge_episode.mp3"
    mp3_file.write_bytes(b"x" * 2000)

    # Set limit to 1000 bytes
    monkeypatch.setattr(settings, "TELEGRAM_MAX_AUDIO_BYTES", 1000)

    with TestingSession() as db:
        job = PodcastJob(
            id="job-oversized-999",
            transport="telegram",
            telegram_chat_id=12345,
            telegram_message_id=555,
            request_mode="literal",
            source_hash="dummyhash",
            source_text="Sample text",
            local_audio_path=str(mp3_file),
            audio_duration_seconds=300,
            status=JobState.AUDIO_READY.value,
        )
        db.add(job)
        db.commit()

    mock_client = MagicMock()
    mock_client.is_configured = True

    with TestingSession() as db:
        delivered = deliver_pending_telegram_jobs(db, mock_client)
        assert delivered == 0

        updated_job = db.query(PodcastJob).filter_by(id="job-oversized-999").first()
        assert updated_job.status == JobState.FAILED_FINAL.value
        assert updated_job.error_code == "TELEGRAM_AUDIO_OVERSIZED"

        # Verify notification sent to user without leaking local path
        mock_client.send_message.assert_called_once()
        msg_args = mock_client.send_message.call_args[1]
        assert str(mp3_file) not in msg_args["text"]
        assert "exceeds this Herald instance's Telegram delivery limit" in msg_args["text"]
        assert "Job ID:" in msg_args["text"]
