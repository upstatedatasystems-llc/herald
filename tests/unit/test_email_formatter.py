import pytest
from herald.services.email_formatter import (
    format_acknowledgment_email,
    format_completion_email,
)


def test_email_formatter_html_escaping_hostile_strings():
    hostile_title = '<script>alert("xss")</script> & "Quoted"'
    hostile_desc = "<iframe src='evil.com'></iframe>"
    hostile_mode = "standard<script>"

    ack = format_acknowledgment_email(
        job_id="job-xss-1",
        episode_title=hostile_title,
        request_mode=hostile_mode,
        estimated_minutes=5.0,
        estimated_completion_range="approximately 5–10 minutes",
    )

    assert "<script>" not in ack["html"]
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in ack["html"]
    assert "job-xss-1" in ack["text"]

    comp = format_completion_email(
        job_id="job-xss-2",
        episode_title=hostile_title,
        episode_description=hostile_desc,
        drive_web_link="https://drive.google.com/file/123",
        duration_seconds=291,
        file_bytes=2335148,
        request_mode="standard",
        source_type="email_body",
        source_title=hostile_title,
        script_estimated_minutes=5.0,
        segments_count=4,
        sha256="abcsha256",
        chunk_count=4,
        retry_attempts=0,
        drive_file_id="drive123",
        source_drive_link="https://drive.google.com/source/123",
        source_drive_id="src123",
        diagnostics_drive_link="https://drive.google.com/diag/123",
        diagnostics_drive_id="diag123",
        created_at_iso="2026-08-07T12:00:00Z",
        completed_at_iso="2026-08-07T12:05:00Z",
        gemini_model="gemini-3.5-flash",
        kokoro_voice="af_heart",
        kokoro_speed=1.0,
    )

    assert "<script>" not in comp["html"]
    assert "<iframe" not in comp["html"]
    assert "&lt;iframe src=&#x27;evil.com&#x27;&gt;&lt;/iframe&gt;" in comp["html"]
    assert "Retry Attempts" in comp["html"]
    assert "Processing attempts" not in comp["html"]
    assert "drive123" in comp["text"]
