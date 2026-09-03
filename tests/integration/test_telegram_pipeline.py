from unittest.mock import MagicMock

from apps.worker import main as worker_main
from herald.config import settings
from herald.db.models import JobState, PodcastJob, TelegramUser
from herald.telegram.auth import (
    generate_pairing_code,
    get_paired_owner,
    verify_and_claim_pairing_code,
)
from herald.telegram.bot import process_telegram_update
from herald.telegram.client import TelegramClient
from herald.telegram.delivery import deliver_pending_telegram_jobs
from herald.tts.kokoro_client import KokoroClient


def test_telegram_end_to_end_literal_pipeline(db_session, tmp_path, monkeypatch):
    """
    End-to-end pipeline test for Telegram-first Literal flow:
    1. Pair owner
    2. Post Telegram message
    3. Verify job queued
    4. Run worker synthesis
    5. Deliver MP3 back to Telegram chat
    """
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "LOCAL_COMPLETE_RETENTION_HOURS", 48)

    # 1. Pairing
    code = generate_pairing_code(db_session)
    success, _ = verify_and_claim_pairing_code(
        db_session, code, user_id=98765, chat_id=98765, username="test_owner"
    )
    assert success is True

    # 2. Intake via Telegram message
    mock_client = MagicMock(spec=TelegramClient)
    update = {
        "update_id": 5001,
        "message": {
            "message_id": 123,
            "from": {"id": 98765, "username": "test_owner"},
            "chat": {"id": 98765},
            "text": "literal\n\n# Autonomous Exploration\nRobotic probes navigate complex terrains.",
        },
    }
    process_telegram_update(db_session, mock_client, update)

    job = db_session.query(PodcastJob).filter(PodcastJob.telegram_message_id == "123").first()
    assert job is not None
    assert job.status == JobState.QUEUED_TTS.value
    assert job.request_mode == "literal"

    # 3. Simulate Kokoro synthesis & FFmpeg audio generation
    fake_wav = tmp_path / "chunk_0.wav"
    fake_wav.write_bytes(b"RIFF" + b"\x00" * 100)

    monkeypatch.setattr(
        "apps.worker.main.process_tts_chunks_parallel",
        lambda **kwargs: [str(fake_wav)],
    )

    fake_mp3 = tmp_path / "output.mp3"
    fake_mp3.write_bytes(b"\xFF\xFB\x90\x44" * 500)

    monkeypatch.setattr(
        "apps.worker.main.join_and_normalize_audio",
        lambda **kwargs: {
            "output_path": str(fake_mp3),
            "file_bytes": len(fake_mp3.read_bytes()),
            "duration_seconds": 45,
            "sha256": "fake-sha-256",
        },
    )

    kokoro_mock = MagicMock(spec=KokoroClient)
    kokoro_mock.health_check.return_value = {"healthy": True}

    # Worker processing
    processed = worker_main.process_next_job(db_session, kokoro_mock, worker_id="test-worker")
    assert processed is True

    db_session.refresh(job)
    assert job.status == JobState.AUDIO_READY.value
    assert job.local_audio_path == str(fake_mp3)

    # 4. Delivery
    deliver_pending_telegram_jobs(db_session, mock_client)

    db_session.refresh(job)
    assert job.status == JobState.COMPLETE.value
    assert job.delivered_at is not None
    mock_client.send_audio.assert_called_once()


def test_application_restart_retains_authorization_and_jobs(db_session):
    """
    Point 22: Application restart retains authorization and queued jobs in database.
    """
    # 1. Authorize owner and queue a job
    owner = TelegramUser(
        telegram_user_id=777888,
        telegram_chat_id="777888",
        username="persisted_owner",
        role="owner",
        is_active=True,
    )
    job = PodcastJob(
        id="persisted-job-1",
        transport="telegram",
        telegram_chat_id="777888",
        telegram_message_id="999",
        status=JobState.QUEUED_TTS.value,
        request_mode="literal",
        source_hash="hash_persist",
        source_text="Persisted source text across restarts.",
    )
    db_session.add(owner)
    db_session.add(job)
    db_session.commit()

    # 2. Simulate fresh DB session after daemon restart
    owner_check = get_paired_owner(db_session)
    assert owner_check is not None
    assert owner_check.telegram_user_id == 777888
    assert owner_check.username == "persisted_owner"

    job_check = db_session.query(PodcastJob).filter(PodcastJob.id == "persisted-job-1").first()
    assert job_check is not None
    assert job_check.status == JobState.QUEUED_TTS.value
    assert job_check.request_mode == "literal"
