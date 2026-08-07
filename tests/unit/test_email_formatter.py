import pytest
from herald.services.email_formatter import (
    format_acknowledgment_email,
    format_completion_email,
    get_canonical_drive_url,
)


def test_get_canonical_drive_url():
    # 1. Web link present -> returns web_link
    assert get_canonical_drive_url("id123", "https://drive.google.com/custom") == "https://drive.google.com/custom"

    # 2. Web link missing, file_id present -> returns canonical fallback URL
    assert get_canonical_drive_url("id123", None) == "https://drive.google.com/file/d/id123/view"
    assert get_canonical_drive_url("id123", "  ") == "https://drive.google.com/file/d/id123/view"

    # 3. Neither present -> returns None
    assert get_canonical_drive_url(None, None) is None
    assert get_canonical_drive_url("", "") is None


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
        drive_web_link="",  # Missing web link -> fallback used
        duration_seconds=286,
        file_bytes=2285148,
        request_mode="brief",
        source_type="email_body",
        source_title=hostile_title,
        script_estimated_minutes=5.0,
        segments_count=4,
        sha256="abcsha256",
        chunk_count=4,
        retry_attempts=0,
        drive_file_id="drive123",
        source_drive_link=None,
        source_drive_id="src123",
        diagnostics_drive_link=None,
        diagnostics_drive_id="diag123",
        created_at_iso="2026-08-07T12:00:00Z",
        completed_at_iso="2026-08-07T12:04:46Z",
        gemini_model="gemini-3.5-flash",
        kokoro_voice="af_heart",
        kokoro_speed=1.0,
    )

    assert "<script>" not in comp["html"]
    assert "<iframe" not in comp["html"]
    assert "&lt;iframe src=&#x27;evil.com&#x27;&gt;&lt;/iframe&gt;" in comp["html"]
    # Fallback canonical links constructed
    assert "https://drive.google.com/file/d/drive123/view" in comp["html"]
    assert "https://drive.google.com/file/d/src123/view" in comp["html"]
    assert "https://drive.google.com/file/d/diag123/view" in comp["html"]
    assert "4m 46s" in comp["html"]
    assert "Brief" in comp["html"]
    assert "Stats for Nerds" in comp["html"]
