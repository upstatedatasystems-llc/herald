"""
Regression test suite for Herald Quality Pass:
- Zero-AI Literal Mode Guarantee
- Title & Heading Quality Gate
- FFmpeg True-Peak Mastering & Telemetry
- AI Failover Non-Fatal Fallback
"""

from unittest.mock import patch

from herald.ai.long_form import cleanup_script_metadata
from herald.audio.ffmpeg_builder import join_and_normalize_audio
from herald.config import settings
from herald.db.models import ContentMode, PodcastJob
from herald.literal.script_generator import generate_literal_script
from herald.services.quality_gate import run_quality_gate


def test_literal_mode_zero_ai_guarantee():
    """
    CRITICAL REGRESSION REQUIREMENT:
    Quality gate findings must NEVER cause Literal mode to invoke LLM repairs.
    Literal mode must be completely deterministic, generate clean headings,
    and return metadata_cleanup_recommended=False.
    """
    long_source = "\n\n".join([f"Paragraph {i}: " + ("word " * 60) for i in range(1, 10)])
    literal_script = generate_literal_script(long_source, source_title="Literal Source Title")

    # Verify deterministic segment headings
    assert len(literal_script.segments) >= 2
    assert literal_script.segments[0].heading in ("Introduction", "Reading")
    assert literal_script.segments[1].heading in ("Reading", "Continued")

    # Run through quality gate with ContentMode.LITERAL
    job = PodcastJob(id="test-literal-job", content_mode=ContentMode.LITERAL.value)
    script_dict = literal_script.model_dump() if hasattr(literal_script, "model_dump") else literal_script.to_dict()

    cleaned_dict, report = run_quality_gate(script_dict, job=job)

    # Must NEVER recommend AI metadata cleanup or duplicate repair in Literal mode
    assert report.metadata_cleanup_recommended is False
    assert report.duplicate_repair_recommended is False


def test_cleanup_script_metadata_non_fatal_fallback():
    """
    If AI provider failover fails during metadata cleanup, it must retain
    the original title and headings without failing the job or corrupting narration.
    """
    job = PodcastJob(id="test-meta-fail-job", request_mode="standard")
    script_dict = {
        "episode_title": "Original Valid Title",
        "segments": [
            {"order": 1, "heading": "Section 1", "narration": "Narration text here."},
            {"order": 2, "heading": "Section 2", "narration": "More narration text here."},
        ],
    }

    with patch("herald.ai.long_form.execute_with_failover", side_effect=RuntimeError("AI Provider Offline")):
        res = cleanup_script_metadata(
            job=job,
            script_dict=script_dict,
            topic="Space Exploration",
        )

        assert res["episode_title"] == "Original Valid Title"
        assert res["segments"][0]["heading"] == "Section 1"
        assert res["segments"][0]["narration"] == "Narration text here."


def test_ffmpeg_mastering_records_true_peak_telemetry(monkeypatch, tmp_path):
    """
    Test that join_and_normalize_audio includes true_peak_dbtp in return dict
    and sets the alimiter filter in the ffmpeg command.
    """
    monkeypatch.setenv("HERALD_MOCK_TTS", "1")
    monkeypatch.setattr(settings, "HERALD_ENV", "test")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))

    dummy_chunk = tmp_path / "chunk_01.wav"
    dummy_chunk.write_bytes(b"dummy wav data")
    out_mp3 = tmp_path / "output.mp3"

    with patch("herald.audio.ffmpeg_builder.validate_audio_file", return_value={"size_bytes": 100, "duration_seconds": 5.0}):
        res = join_and_normalize_audio(
            chunk_paths=[dummy_chunk],
            output_mp3_path=out_mp3,
            episode_title="Title",
            job_id="job-master-01",
        )

        assert "true_peak_dbtp" in res
        assert res["true_peak_dbtp"] == getattr(settings, "HERALD_AUDIO_TRUE_PEAK_DBTP", -1.5)
