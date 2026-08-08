import json
from datetime import UTC, datetime
from pathlib import Path
import pytest

from herald.audio.artifact_generator import (
    ensure_source_artifact,
    generate_diagnostics_artifact,
    get_artifact_filenames,
    get_required_artifact_types,
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
    assert names["diagnostics_filename"].endswith("_diagnostics.md")
    assert "test_episode_title" in names["audio_filename"]


def test_get_required_artifact_types():
    job_brief = PodcastJob(request_mode=RequestMode.BRIEF.value, script_json={"segments": []})
    assert get_required_artifact_types(job_brief) == ["audio", "source", "diagnostics", "script"]

    job_research = PodcastJob(request_mode=RequestMode.RESEARCH.value, research_json={"source_summary": "Summary"})
    assert get_required_artifact_types(job_research) == [
        "audio",
        "source",
        "diagnostics",
        "script",
        "research",
        "research_notes",
    ]


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


def test_generate_diagnostics_artifact_excludes_secrets_and_formats_markdown(tmp_path):
    job = PodcastJob(
        id="job-diag-001",
        gmail_message_id="msg-diag-1",
        sender_email="auth@example.com",
        request_mode=RequestMode.BRIEF.value,
        source_type=SourceType.EMAIL_BODY.value,
        source_hash="diaghash1",
        source_text="Full original source text paragraph.",
        status=JobState.AUDIO_READY.value,
        completed_chunk_index=4,
        local_audio_path=str(tmp_path / "test.mp3"),
        audio_bytes=2048500,
        audio_sha256="abc123sha",
        audio_duration_seconds=180,
        drive_file_id="drive-audio-id-123",
        source_drive_file_id="drive-source-id-456",
        diagnostics_drive_file_id="drive-diag-id-789",
        created_at=datetime.now(UTC),
        audio_ready_at=datetime.now(UTC),
        script_json={
            "episode_title": "Diag Test Title",
            "episode_description": "Episode description text.",
            "segments": [{"order": 1, "heading": "Introduction", "narration": "Hello world narration."}],
            "warnings": [],
        },
    )
    path = generate_diagnostics_artifact(job, tmp_path)
    assert path.exists()
    assert path.name.endswith("_diagnostics.md")

    md_text = path.read_text(encoding="utf-8")

    assert "# Herald Run Diagnostics" in md_text
    assert "## Episode" in md_text
    assert "## Output Summary" in md_text
    assert "## Content Metrics" in md_text
    assert "## Original Source" in md_text
    assert "## Final Podcast Script" in md_text
    assert "## Technical Identifiers" in md_text

    # Verify no raw JSON syntax wrapping the script
    assert "#### Segment 1: Introduction" in md_text
    assert "Hello world narration." in md_text
    assert '{"order": 1' not in md_text

    # Verify non-Research job omits Research section
    assert "## Research Summary" not in md_text
    assert "## Research Sources" not in md_text

    # Verify secrets and HTML/CSS excluded
    assert "api_key" not in md_text.lower()
    assert "renderedcontent" not in md_text.lower()

