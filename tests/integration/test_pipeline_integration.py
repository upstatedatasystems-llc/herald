import os

from packages.herald.audio.ffmpeg_builder import join_and_normalize_audio
from packages.herald.db.models import JobState, PodcastJob, RequestMode, SourceType
from packages.herald.db.state_machine import transition_job_state
from packages.herald.extraction.email_parser import process_email_message
from packages.herald.gemini.schema import PodcastScriptResponse
from packages.herald.tts.kokoro_client import KokoroClient


def test_full_mocked_pipeline_integration(db_session, tmp_path):
    """
    Test complete vertical slice:
    intake -> validation -> scripting -> queue -> TTS synthesis -> FFmpeg MP3 encoding -> delivery metadata -> COMPLETE
    """
    os.environ["HERALD_MOCK_TTS"] = "1"

    # Step 1: Intake email message
    raw_subject = "Podcast: Standard"
    raw_body = "Today in technology, cloud hardware models are expanding rapidly with ARM architecture."
    sender = "authorized@example.com"
    msg_id = "gmail-msg-test-999"

    parsed = process_email_message(subject=raw_subject, body_text=raw_body)
    assert parsed.mode == RequestMode.STANDARD

    job = PodcastJob(
        gmail_message_id=msg_id,
        sender_email=sender,
        request_mode=parsed.mode.value,
        source_type=SourceType.EMAIL_BODY.value,
        source_hash=parsed.source_hash,
        source_text=parsed.clean_text,
        status=JobState.RECEIVED.value,
    )
    db_session.add(job)
    db_session.commit()

    # Step 2: Validate & Extract
    transition_job_state(db_session, job, JobState.VALIDATING.value, component="test")
    transition_job_state(db_session, job, JobState.SOURCE_READY.value, component="test")
    assert job.status == JobState.SOURCE_READY.value

    # Step 3: Scripting with Gemini mock script
    script_data = {
        "episode_title": "ARM Architecture Breakdown",
        "episode_description": "A quick overview of cloud ARM computing.",
        "requested_mode": "standard",
        "segments": [
            {"sequence": 1, "speaker": "host", "text": "Welcome to Herald. Today we discuss cloud ARM computing."},
            {"sequence": 2, "speaker": "host", "text": "Ampere A1 servers provide great efficiency for continuous jobs."},
        ],
    }
    validated_script = PodcastScriptResponse(**script_data)
    job.script_json = validated_script.model_dump()
    db_session.commit()

    transition_job_state(db_session, job, JobState.SCRIPTING.value, component="test")
    transition_job_state(db_session, job, JobState.SCRIPT_READY.value, component="test")
    transition_job_state(db_session, job, JobState.QUEUED.value, component="test")
    assert job.status == JobState.QUEUED.value

    # Step 4: Worker TTS synthesis & FFmpeg audio assembly
    transition_job_state(db_session, job, JobState.SYNTHESIZING.value, component="test")

    client = KokoroClient()
    chunk_1 = tmp_path / "chunk_0001.wav"
    chunk_2 = tmp_path / "chunk_0002.wav"

    client.synthesize_chunk("Welcome to Herald. Today we discuss cloud ARM computing.", chunk_1)
    client.synthesize_chunk("Ampere A1 servers provide great efficiency for continuous jobs.", chunk_2)

    assert chunk_1.exists() and chunk_1.stat().st_size > 0
    assert chunk_2.exists() and chunk_2.stat().st_size > 0

    transition_job_state(db_session, job, JobState.ENCODING.value, component="test")

    output_mp3 = tmp_path / "test_episode.mp3"
    audio_info = join_and_normalize_audio(
        chunk_paths=[chunk_1, chunk_2],
        output_mp3_path=output_mp3,
        episode_title=validated_script.episode_title,
        episode_description=validated_script.episode_description,
        job_id=job.id,
    )

    job.local_audio_path = audio_info["output_path"]
    job.audio_bytes = audio_info["file_bytes"]
    job.audio_duration_seconds = audio_info["duration_seconds"]
    job.audio_sha256 = audio_info["sha256"]
    db_session.commit()

    transition_job_state(db_session, job, JobState.AUDIO_READY.value, component="test")
    assert job.status == JobState.AUDIO_READY.value
    assert output_mp3.exists()

    # Step 5: Drive Upload & Delivery Completion
    transition_job_state(db_session, job, JobState.UPLOADING.value, component="test")
    job.drive_file_id = "drive-file-xyz-123"
    job.drive_web_link = "https://drive.google.com/file/d/drive-file-xyz-123/view"
    db_session.commit()

    transition_job_state(db_session, job, JobState.DELIVERING.value, component="test")
    transition_job_state(db_session, job, JobState.COMPLETE.value, component="test")

    assert job.status == JobState.COMPLETE.value
    assert job.completed_at is not None
    assert job.drive_file_id == "drive-file-xyz-123"
