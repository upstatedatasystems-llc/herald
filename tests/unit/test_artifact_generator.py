from datetime import UTC, datetime

from herald.audio.artifact_generator import (
    ensure_details_artifact,
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
    assert names["details_filename"].endswith("_details.md")
    assert "test_episode_title" in names["audio_filename"]


def test_get_required_artifact_types():
    job_brief = PodcastJob(request_mode=RequestMode.BRIEF.value, script_json={"segments": []})
    assert get_required_artifact_types(job_brief) == ["audio", "details"]

    job_research = PodcastJob(
        request_mode=RequestMode.RESEARCH.value, research_json={"source_summary": "Summary"}
    )
    assert get_required_artifact_types(job_research) == ["audio", "details"]


def test_ensure_details_artifact(tmp_path):
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
        script_json={
            "episode_title": "Article Podcast",
            "segments": [{"order": 1, "heading": "Intro", "narration": "Welcome to the podcast."}],
        },
    )
    path = ensure_details_artifact(job, tmp_path)
    assert path.exists()
    assert path.name.endswith("_details.md")
    content = path.read_text(encoding="utf-8")
    assert "https://example.com/article" in content
    assert "Clean extracted article text" in content
    assert "### Structured Script JSON" in content
    assert "```json" in content


def test_details_artifact_completeness_and_secrets_exclusion(tmp_path):
    job = PodcastJob(
        id="job-diag-001",
        gmail_message_id="msg-diag-1",
        sender_email="auth@example.com",
        request_mode=RequestMode.RESEARCH.value,
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
        details_drive_file_id="drive-details-id-456",
        created_at=datetime.now(UTC),
        audio_ready_at=datetime.now(UTC),
        research_json={
            "source_summary": "Deep summary of research topic.",
            "claims_and_evidence": [],
            "key_entities": [],
            "core_narrative_arc": "Arc",
            "open_questions": [],
            "grounded_sources": [{"title": "Source 1", "url": "https://source1.org"}],
        },
        research_audit_json={"has_material_issues": False, "findings": []},
        script_json={
            "episode_title": "Diag Test Title",
            "episode_description": "Episode description text.",
            "segments": [
                {"order": 1, "heading": "Introduction", "narration": "Hello world narration."}
            ],
            "warnings": [],
        },
    )
    path = ensure_details_artifact(job, tmp_path)
    assert path.exists()
    assert path.name.endswith("_details.md")

    md_text = path.read_text(encoding="utf-8")

    assert "# Herald Episode Details" in md_text
    assert "## Episode" in md_text
    assert "## Processing Summary" in md_text
    assert "## Content Metrics" in md_text
    assert "## Original Source" in md_text
    assert "## Final Podcast Script" in md_text
    assert "### Structured Script JSON" in md_text
    assert "## Research Investigation Summary" in md_text
    assert "## Technical Identifiers" in md_text

    # Verify secrets and auth credentials excluded
    assert "api_key" not in md_text.lower()
    assert "auth_token" not in md_text.lower()
