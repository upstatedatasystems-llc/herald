from datetime import UTC, datetime

from herald.db.models import PodcastJob, RequestMode
from herald.telegram.formatters import (
    format_approval,
    format_completion,
    format_queued,
    get_job_ai_identity,
)


def test_ai_identity_truthfulness():
    """Verify truthful provider/model labeling for Literal, Research, Brief, and Standard modes."""
    # 1. Literal -> No AI provider/model
    literal_job = PodcastJob(id="j1", request_mode=RequestMode.LITERAL.value)
    prov, model = get_job_ai_identity(literal_job)
    assert prov is None
    assert model is None

    # 2. Research -> Gemini + research model
    research_job = PodcastJob(
        id="j2",
        request_mode=RequestMode.RESEARCH.value,
        research_model="gemini-2.5-flash",
    )
    prov, model = get_job_ai_identity(research_job)
    assert prov == "Gemini"
    assert model == "gemini-2.5-flash"

    # 3. Standard / Brief -> Gemini + standard model
    std_job = PodcastJob(
        id="j3",
        request_mode=RequestMode.STANDARD.value,
        gemini_model="gemini-3.5-flash",
    )
    prov, model = get_job_ai_identity(std_job)
    assert prov == "Gemini"
    assert model == "gemini-3.5-flash"


def test_formatters_truthful_and_html_escaped():
    """Formatters output truthful snapshot values and properly escape HTML."""
    now = datetime.now(UTC)
    job = PodcastJob(
        id="test-job-uuid-12345",
        request_mode="literal",
        custom_voice="af_bella",
        custom_speed=1.1,
        custom_title="Special <Escaped> & Title",
        source_text="Word " * 50,
        script_json={
            "episode_title": "Special <Escaped> & Title",
            "episode_description": "A <test> & summary of the article.",
            "segments": [{"narration": "Narration text with <entities> & more."}],
        },
        audio_duration_seconds=45,
        created_at=now,
        completed_at=now,
    )

    # 1. Approval card
    app_text, markup = format_approval(job, job.script_json, eta_info={"estimated_completion_range": "3–5 minutes"})
    assert "Special &lt;Escaped&gt; &amp; Title" in app_text
    assert "af_bella" in app_text
    assert "1.1x" in app_text
    assert "AI Model:" not in app_text  # Literal mode must NOT label AI
    assert "h2:approve:test-job-uuid-12345" in markup["inline_keyboard"][0][0]["callback_data"]

    # 2. Queued card
    q_text = format_queued(job, job.script_json, eta_info={"jobs_ahead": 0, "estimated_completion_range": "3–5 minutes"})
    assert "Special &lt;Escaped&gt; &amp; Title" in q_text
    assert "af_bella" in q_text
    assert "1.1x" in q_text
    assert "AI Model:" not in q_text

    # 3. Completion card (caption)
    comp_text = format_completion(job, actual_chunks_count=1, file_size_bytes=500_000)
    assert "Special &lt;Escaped&gt; &amp; Title" in comp_text
    assert "af_bella" in comp_text
    assert "1.1x" in comp_text
    assert "AI Model:" not in comp_text


def test_max_length_caption_stays_within_telegram_1024_limit():
    """Very long titles/descriptions are truncated safely so caption stays well within 1024 characters."""
    now = datetime.now(UTC)
    huge_desc = "This is an extremely long description designed to test boundary limits. " * 20
    job = PodcastJob(
        id="test-long-job-id-12345",
        request_mode="research",
        research_depth="high",
        research_model="gemini-2.5-flash",
        custom_voice="af_bella",
        custom_speed=1.0,
        custom_title="A" * 150,
        script_json={
            "episode_title": "A" * 150,
            "episode_description": huge_desc,
            "segments": [{"narration": "Test"}],
        },
        audio_duration_seconds=3600,
        created_at=now,
        completed_at=now,
    )

    caption = format_completion(job, actual_chunks_count=25, file_size_bytes=10 * 1024 * 1024)
    assert len(caption) < 1024
    assert len(caption.encode("utf-8")) < 1024
