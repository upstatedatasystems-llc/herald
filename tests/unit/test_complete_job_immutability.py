import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.core.models import HeraldRequest
from herald.core.pipeline import compute_content_hash, process_herald_request
from herald.db.models import Base, JobState, PodcastJob


def test_complete_job_immutability_and_cleaned_up_audio(tmp_path):
    """
    Test that:
    1. If a matching COMPLETE job exists and local MP3 is present, it is returned as duplicate.
    2. If the local MP3 is no longer present on disk, a NEW job is created and the old COMPLETE job remains immutable.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    mp3_file = tmp_path / "old_audio.mp3"
    mp3_file.write_bytes(b"audio data")

    source = "Consistent source text."
    real_hash = compute_content_hash(source, None)

    with TestingSession() as db:
        old_job = PodcastJob(
            id="job-complete-old",
            transport="email",
            sender_email="user@example.com",
            request_mode="literal",
            source_hash=real_hash,
            source_text=source,
            custom_title="Episode Title",
            local_audio_path=str(mp3_file),
            status=JobState.COMPLETE.value,
        )
        db.add(old_job)
        db.commit()

        # Case 1: Audio file is present on disk -> duplicate returned
        req1 = HeraldRequest(
            transport="telegram",
            requester_identity="telegram:999",
            delivery_target="999",
            mode="literal",
            source_text=source,
            custom_title="Episode Title",
        )
        res1 = process_herald_request(db, req1)
        assert res1.is_duplicate is True
        assert res1.job_id == "job-complete-old"
        assert res1.status == JobState.COMPLETE.value

        # Verify old_job was NOT mutated
        reloaded_old = db.query(PodcastJob).filter_by(id="job-complete-old").first()
        assert reloaded_old.transport == "email"
        assert reloaded_old.status == JobState.COMPLETE.value

        # Case 2: Delete audio file to simulate retention cleanup
        os.remove(mp3_file)

        req2 = HeraldRequest(
            transport="telegram",
            transport_message_id="777",
            requester_identity="telegram:999",
            delivery_target="999",
            mode="literal",
            source_text=source,
            custom_title="Episode Title",
        )
        res2 = process_herald_request(db, req2)
        assert res2.is_duplicate is False
        assert res2.job_id != "job-complete-old"

        # Verify old job still exists and is still COMPLETE
        reloaded_old2 = db.query(PodcastJob).filter_by(id="job-complete-old").first()
        assert reloaded_old2.status == JobState.COMPLETE.value
        assert reloaded_old2.transport == "email"

        # Verify new job was created
        new_job = db.query(PodcastJob).filter_by(id=res2.job_id).first()
        assert new_job is not None
        assert new_job.transport == "telegram"
        assert new_job.telegram_chat_id == 999
