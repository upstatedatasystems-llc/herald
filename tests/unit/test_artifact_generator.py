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

    job_research = PodcastJob(request_mode=RequestMode.RESEARCH.value, research_json={"source_summary": "Summary"})
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
            "segments": [{"order": 1, "heading": "Introduction", "narration": "Hello world narration."}],
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


def test_verify_and_research_audit_status_truthfulness(tmp_path):
    # Case 1: verify requested, but no verify_audit_json -> NOT COMPLETED / UNAVAILABLE
    job1 = PodcastJob(
        id="job-truth-1",
        request_mode=RequestMode.STANDARD.value,
        verify_final_script=True,
        verify_audit_json=None,
        script_json={"episode_title": "T1", "segments": []},
        created_at=datetime.now(UTC),
    )
    p1 = ensure_details_artifact(job1, tmp_path)
    text1 = p1.read_text(encoding="utf-8")
    assert "- **Fidelity Verify Status**: `NOT COMPLETED / UNAVAILABLE`" in text1

    # Case 2: verify requested, audit has no material issues -> PASS
    job2 = PodcastJob(
        id="job-truth-2",
        request_mode=RequestMode.STANDARD.value,
        verify_final_script=True,
        verify_audit_json={"has_material_issues": False},
        script_json={"episode_title": "T2", "segments": []},
        created_at=datetime.now(UTC),
    )
    p2 = ensure_details_artifact(job2, tmp_path)
    text2 = p2.read_text(encoding="utf-8")
    assert "- **Fidelity Verify Status**: `PASS`" in text2

    # Case 3: verify requested, audit has material issues, repair_count=1 -> REPAIRED (1 pass)
    job3 = PodcastJob(
        id="job-truth-3",
        request_mode=RequestMode.STANDARD.value,
        verify_final_script=True,
        verify_audit_json={"has_material_issues": True},
        verify_repair_count=1,
        script_json={"episode_title": "T3", "segments": []},
        created_at=datetime.now(UTC),
    )
    p3 = ensure_details_artifact(job3, tmp_path)
    text3 = p3.read_text(encoding="utf-8")
    assert "- **Fidelity Verify Status**: `REPAIRED (1 pass)`" in text3

    # Case 4: Research mode, no research_audit_json -> NOT COMPLETED / UNAVAILABLE
    job4 = PodcastJob(
        id="job-truth-4",
        request_mode=RequestMode.RESEARCH.value,
        research_audit_json=None,
        script_json={"episode_title": "T4", "segments": []},
        created_at=datetime.now(UTC),
    )
    p4 = ensure_details_artifact(job4, tmp_path)
    text4 = p4.read_text(encoding="utf-8")
    assert "- **Research Audit Status**: `NOT COMPLETED / UNAVAILABLE`" in text4

    # Case 5: Research mode, research_audit_json with no material issues -> PASS
    job5 = PodcastJob(
        id="job-truth-5",
        request_mode=RequestMode.RESEARCH.value,
        research_audit_json={"has_material_issues": False},
        script_json={"episode_title": "T5", "segments": []},
        created_at=datetime.now(UTC),
    )
    p5 = ensure_details_artifact(job5, tmp_path)
    text5 = p5.read_text(encoding="utf-8")
    assert "- **Research Audit Status**: `PASS`" in text5

    # Case 6: Research mode, research_audit_json with material issues and repair_count=1 -> REPAIRED (1 pass)
    job6 = PodcastJob(
        id="job-truth-6",
        request_mode=RequestMode.RESEARCH.value,
        research_audit_json={"has_material_issues": True},
        research_repair_count=1,
        script_json={"episode_title": "T6", "segments": []},
        created_at=datetime.now(UTC),
    )
    p6 = ensure_details_artifact(job6, tmp_path)
    text6 = p6.read_text(encoding="utf-8")
    assert "- **Research Audit Status**: `REPAIRED (1 pass)`" in text6
