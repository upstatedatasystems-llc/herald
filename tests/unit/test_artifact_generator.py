import json
from datetime import UTC, datetime
from pathlib import Path
import pytest

from herald.audio.artifact_generator import (
    ensure_source_artifact,
    generate_diagnostics_artifact,
    get_artifact_filenames,
)
from herald.db.models import JobState, PodcastJob, RequestMode, SourceType


def test_artifact_filenames_generation():
    job = PodcastJob(
        id="12345678-aaaa-bbbb-cccc-ddddeeeeffff",
        custom_title="Test Episode Title",
        created_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
    )
    names = get_artifact_filenames(job)
    assert names["audio_filename"].endswith(".mp3")
    assert names["source_filename"].endswith("_source.txt")
    assert names["diagnostics_filename"].endswith("_diagnostics.json")
    assert "test_episode_title" in names["audio_filename"]


def test_ensure_source_artifact(tmp_path):
    job = PodcastJob(
        id="job-src-001",
        gmail_message_id="msg-src-1",
        sender_email="auth@example.com",
        request_mode=RequestMode.STANDARD.value,
        source_type=SourceType.URL.value,
        source_url="https://example.com/article",
        source_hash="srchash1",
        source_text="Clean extracted article text",
        custom_title="Article Podcast",
        created_at=datetime.now(UTC),
    )
    path = ensure_source_artifact(job, tmp_path)
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "Source URL: https://example.com/article" in content
    assert "Clean extracted article text" in content
    assert "secret" not in content.lower()


def test_generate_diagnostics_artifact_excludes_secrets(tmp_path):
    job = PodcastJob(
        id="job-diag-001",
        gmail_message_id="msg-diag-1",
        sender_email="auth@example.com",
        request_mode=RequestMode.BRIEF.value,
        source_type=SourceType.EMAIL_BODY.value,
        source_hash="diaghash1",
        source_text="Email text",
        status=JobState.AUDIO_READY.value,
        completed_chunk_index=4,
        local_audio_path=str(tmp_path / "test.mp3"),
        audio_bytes=2048500,
        audio_sha256="abc123sha",
        audio_duration_seconds=180,
        drive_file_id="drive-audio-id-123",
        source_drive_file_id="drive-source-id-456",
        diagnostics_drive_file_id="drive-diag-id-789",  # Should NOT be inside its own JSON
        created_at=datetime.now(UTC),
        audio_ready_at=datetime.now(UTC),
        script_json={
            "episode_title": "Diag Test Title",
            "episode_description": "Desc",
            "estimated_minutes": 3.0,
            "segments": [{}, {}, {}, {}],
            "warnings": [],
        },
    )
    path = generate_diagnostics_artifact(job, tmp_path)
    assert path.exists()
    diag_json = json.loads(path.read_text(encoding="utf-8"))

    assert diag_json["job_id"] == "job-diag-001"
    assert diag_json["drive"]["audio_file_id"] == "drive-audio-id-123"
    assert diag_json["drive"]["source_file_id"] == "drive-source-id-456"
    assert "diagnostics_file_id" not in diag_json["drive"]
    assert "api_key" not in str(diag_json).lower()
