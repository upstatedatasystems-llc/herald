import threading
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.db.models import Base, JobState, PodcastJob
from herald.telegram.client import TelegramClient
from herald.telegram.delivery import deliver_pending_telegram_jobs


def test_telegram_delivery_claiming_and_loop_processing(tmp_path):
    """
    Test delivery claiming semantics:
    Ensure deliver_pending_telegram_jobs claims eligible jobs one-by-one,
    transitions them to DELIVERING, and delivers all ready jobs without duplication.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    # Create 3 ready jobs with dummy audio files
    audio_file1 = tmp_path / "ep1.mp3"
    audio_file2 = tmp_path / "ep2.mp3"
    audio_file3 = tmp_path / "ep3.mp3"
    for f in (audio_file1, audio_file2, audio_file3):
        f.write_bytes(b"dummy mp3 data")

    with TestingSession() as db:
        j1 = PodcastJob(
            id="job-c1",
            transport="telegram",
            telegram_chat_id=101,
            telegram_message_id=1,
            request_mode="literal",
            source_hash="hash1",
            source_text="Text 1",
            local_audio_path=str(audio_file1),
            audio_duration_seconds=60,
            status=JobState.AUDIO_READY.value,
        )
        j2 = PodcastJob(
            id="job-c2",
            transport="telegram",
            telegram_chat_id=102,
            telegram_message_id=2,
            request_mode="literal",
            source_hash="hash2",
            source_text="Text 2",
            local_audio_path=str(audio_file2),
            audio_duration_seconds=70,
            status=JobState.AUDIO_READY.value,
        )
        j3 = PodcastJob(
            id="job-c3",
            transport="telegram",
            telegram_chat_id=103,
            telegram_message_id=3,
            request_mode="literal",
            source_hash="hash3",
            source_text="Text 3",
            local_audio_path=str(audio_file3),
            audio_duration_seconds=80,
            status=JobState.AUDIO_READY.value,
        )
        db.add_all([j1, j2, j3])
        db.commit()

    sent_job_ids = []
    lock = threading.Lock()

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.is_configured = True

    def mock_send_audio(chat_id, audio_path, **kwargs):
        with lock:
            sent_job_ids.append(chat_id)
        return {"message_id": 999}

    mock_client.send_audio.side_effect = mock_send_audio

    with TestingSession() as db:
        delivered = deliver_pending_telegram_jobs(db, mock_client)
        assert delivered == 3

    with TestingSession() as db:
        completed = db.query(PodcastJob).filter(PodcastJob.status == JobState.COMPLETE.value).all()
        assert len(completed) == 3
        # Each distinct chat received exactly one audio file
        assert sorted(sent_job_ids) == [101, 102, 103]
        assert len(sent_job_ids) == 3
