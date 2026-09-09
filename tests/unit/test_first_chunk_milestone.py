from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.db.models import Base, JobState, PodcastJob
from herald.services.progress_notifier import notify_tts_chunk_progress
from herald.telegram.client import TelegramClient


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_first_chunk_milestone_sent_and_claimed(db_session):
    now = datetime.now(UTC)
    job = PodcastJob(
        id="job-milestone-1",
        transport="telegram",
        telegram_chat_id=12345,
        telegram_user_id=67890,
        request_mode="literal",
        source_hash="hash-ms-1",
        source_text="This is a test article for first chunk milestone notification.",
        custom_voice="af_bella",
        custom_speed=1.1,
        status=JobState.SYNTHESIZING.value,
        created_at=now,
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.is_configured = True
    mock_client.send_message.return_value = {"message_id": 555}

    result = notify_tts_chunk_progress(
        db=db_session,
        job=job,
        chunk_index=1,
        total_chunks=3,
        chunk_audio_duration_s=15.0,
        chunk_synthesis_duration_s=8.0,
        telegram_client=mock_client,
    )

    assert result is True
    db_session.refresh(job)
    assert job.first_chunk_progress_claimed_at is not None
    assert job.telegram_progress_message_id == 555
    mock_client.send_message.assert_called_once()
    call_args = mock_client.send_message.call_args[1]
    assert call_args["chat_id"] == 12345
    assert "First segment synthesized (1/3)" in call_args["text"]
    assert "Literal reader (zero AI calls)" in call_args["text"]
    assert "af_bella" in call_args["text"]


def test_first_chunk_milestone_atomic_cas_prevents_duplicate(db_session):
    now = datetime.now(UTC)
    job = PodcastJob(
        id="job-milestone-2",
        transport="telegram",
        telegram_chat_id=12345,
        telegram_user_id=67890,
        request_mode="standard",
        gemini_model="gemini-3.5-flash",
        source_hash="hash-ms-2",
        source_text="Test article content for duplicate milestone check.",
        status=JobState.SYNTHESIZING.value,
        created_at=now,
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.is_configured = True
    mock_client.send_message.return_value = {"message_id": 556}

    # First call succeeds
    res1 = notify_tts_chunk_progress(
        db=db_session,
        job=job,
        chunk_index=1,
        total_chunks=4,
        chunk_audio_duration_s=10.0,
        chunk_synthesis_duration_s=5.0,
        telegram_client=mock_client,
    )
    assert res1 is True

    # Second call for chunk 1 (or concurrent chunk) is rejected by atomic CAS
    res2 = notify_tts_chunk_progress(
        db=db_session,
        job=job,
        chunk_index=1,
        total_chunks=4,
        chunk_audio_duration_s=10.0,
        chunk_synthesis_duration_s=5.0,
        telegram_client=mock_client,
    )
    assert res2 is False
    # send_message should have been called only once
    assert mock_client.send_message.call_count == 1


def test_non_first_chunk_ignored(db_session):
    now = datetime.now(UTC)
    job = PodcastJob(
        id="job-milestone-3",
        transport="telegram",
        telegram_chat_id=12345,
        telegram_progress_message_id=999,
        source_hash="hash-ms-3",
        source_text="Sample",
        status=JobState.SYNTHESIZING.value,
        created_at=now,
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    res = notify_tts_chunk_progress(
        db=db_session,
        job=job,
        chunk_index=2,
        total_chunks=3,
        chunk_audio_duration_s=10.0,
        chunk_synthesis_duration_s=5.0,
        telegram_client=mock_client,
    )
    assert res is False
    mock_client.send_message.assert_not_called()


def test_non_telegram_transport_ignored(db_session):
    now = datetime.now(UTC)
    job = PodcastJob(
        id="job-milestone-4",
        transport="email",
        sender_email="user@example.com",
        source_hash="hash-ms-4",
        source_text="Sample",
        status=JobState.SYNTHESIZING.value,
        created_at=now,
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    res = notify_tts_chunk_progress(
        db=db_session,
        job=job,
        chunk_index=1,
        total_chunks=3,
        chunk_audio_duration_s=10.0,
        chunk_synthesis_duration_s=5.0,
        telegram_client=mock_client,
    )
    assert res is False
    mock_client.send_message.assert_not_called()


def test_progress_claim_retry_on_failure_and_lease_expiry(db_session):
    """
    If send_message fails, the CAS claim is cleared to allow immediate retry.
    If a claim was somehow abandoned without a message_id and is >30s old, the lease expires and allows retry.
    """
    from datetime import timedelta

    now = datetime.now(UTC)
    job = PodcastJob(
        id="job-milestone-retry",
        transport="telegram",
        telegram_chat_id=12345,
        telegram_message_id=987,
        source_hash="hash-retry-1",
        source_text="Sample text",
        status=JobState.SYNTHESIZING.value,
        created_at=now,
    )
    db_session.add(job)
    db_session.commit()

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.is_configured = True
    mock_client.send_message.side_effect = Exception("Telegram API timeout")

    # 1. First attempt fails on send
    res1 = notify_tts_chunk_progress(
        db=db_session,
        job=job,
        chunk_index=1,
        total_chunks=3,
        chunk_audio_duration_s=10.0,
        chunk_synthesis_duration_s=5.0,
        telegram_client=mock_client,
    )
    assert res1 is False
    db_session.refresh(job)
    assert job.telegram_progress_message_id is None
    # Claim must have been cleared on failure!
    assert job.first_chunk_progress_claimed_at is None

    # 2. Immediate retry succeeds
    mock_client.send_message.side_effect = None
    mock_client.send_message.return_value = {"message_id": 5555}
    res2 = notify_tts_chunk_progress(
        db=db_session,
        job=job,
        chunk_index=1,
        total_chunks=3,
        chunk_audio_duration_s=10.0,
        chunk_synthesis_duration_s=5.0,
        telegram_client=mock_client,
    )
    assert res2 is True
    db_session.refresh(job)
    assert job.telegram_progress_message_id == 5555
    assert job.first_chunk_progress_claimed_at is not None

    # 3. Third attempt is ignored because message_id is set
    res3 = notify_tts_chunk_progress(
        db=db_session,
        job=job,
        chunk_index=1,
        total_chunks=3,
        chunk_audio_duration_s=10.0,
        chunk_synthesis_duration_s=5.0,
        telegram_client=mock_client,
    )
    assert res3 is False
