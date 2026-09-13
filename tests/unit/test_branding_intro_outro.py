"""Unit tests for Deterministic Herald Intro/Outro Branding.
Tests:
- Fixed duration template rendering
- Auto/None/Literal template rendering
- Outro template immutability
- Topic sanitization edge cases
- Branding segments are application-owned and not from LLM
"""

from herald.audio.branding import (
    INTRO_FIXED_TEMPLATE,
    INTRO_GENERAL_TEMPLATE,
    OUTRO_TEMPLATE,
    render_intro_narration,
    render_outro_narration,
    sanitize_branding_topic,
)


def test_intro_template_fixed_duration():
    intro_10 = render_intro_narration("Virginia-class submarines", target_minutes="10")
    assert "approximately 10-minute podcast about Virginia-class submarines" in intro_10
    assert intro_10.startswith("This is Herald, an open-source podcast generation platform.")
    assert intro_10.endswith("Enjoy.")

    intro_45 = render_intro_narration("Quantum Computing", target_minutes=45)
    assert "approximately 45-minute podcast about Quantum Computing" in intro_45


def test_intro_template_auto_and_literal():
    intro_auto = render_intro_narration("James Webb Space Telescope", target_minutes="auto")
    assert "listening to a podcast about James Webb Space Telescope" in intro_auto
    assert "approximately" not in intro_auto

    intro_none = render_intro_narration("Mars Rover", target_minutes=None)
    assert "listening to a podcast about Mars Rover" in intro_none
    assert "approximately" not in intro_none

    intro_zero = render_intro_narration("Deep Sea Exploration", target_minutes="0")
    assert "listening to a podcast about Deep Sea Exploration" in intro_zero
    assert "approximately" not in intro_zero


def test_outro_template():
    from herald.config import settings
    outro = render_outro_narration()
    expected = OUTRO_TEMPLATE.format(platform_name=settings.BRANDING_PLATFORM_NAME)
    assert outro == expected
    assert "Herald" in outro or settings.BRANDING_PLATFORM_NAME in outro


def test_topic_sanitization():
    assert sanitize_branding_topic(None) == "today's topic"
    assert sanitize_branding_topic("") == "today's topic"
    assert sanitize_branding_topic("   ") == "today's topic"

    # Strip HTML tags
    assert sanitize_branding_topic("<b>Artificial Intelligence</b>") == "Artificial Intelligence"

    # Strip markdown
    assert sanitize_branding_topic("[Submarines](https://example.com/sub)") == "Submarines"
    assert sanitize_branding_topic("**Virginia-Class** *Submarines*") == "Virginia-Class Submarines"

    # Strip punctuation / whitespace
    assert sanitize_branding_topic("  ...Electric Vehicles!  ") == "Electric Vehicles"

    # Truncate overly long topics at word boundaries
    long_topic = "A Very Long Analysis of Global Economic Policy and Financial Markets During the Mid-Twenty-First Century Transition"
    cleaned = sanitize_branding_topic(long_topic, max_chars=40)
    assert len(cleaned) <= 40
    assert not cleaned.endswith(" ")


def test_url_topic_sanitization():
    # Full URL with path and query parameters
    url = "https://example.com/2026/09/12/breakthrough-in-fusion-power?utm_source=rss&ref=twitter"
    clean_url_topic = sanitize_branding_topic(url)
    assert clean_url_topic == "Breakthrough In Fusion Power"
    assert "https" not in clean_url_topic
    assert "utm" not in clean_url_topic

    # Root domain URL
    domain_url = "https://arstechnica.com/"
    assert sanitize_branding_topic(domain_url) == "Arstechnica"


def test_truthful_duration_reporting_when_underfilled():
    # User requested 45 minutes, but actual body was only 320 seconds (~5 minutes)
    # The intro narration MUST truthfully state approximately 5 minutes, NOT 45 minutes!
    intro_underfilled = render_intro_narration(
        topic="Solid State Batteries",
        target_minutes="45",
        actual_body_duration_seconds=320.0,
    )
    assert "approximately 5-minute podcast about Solid State Batteries" in intro_underfilled
    assert "45" not in intro_underfilled

    # If actual body matches target (~44.5 min = 2670s for 45 min target)
    intro_matched = render_intro_narration(
        topic="Solid State Batteries",
        target_minutes="45",
        actual_body_duration_seconds=2670.0,
    )
    assert "approximately 45-minute podcast about Solid State Batteries" in intro_matched


def test_synthesize_branding_segment_contract(tmp_path):
    from unittest.mock import MagicMock
    from herald.audio.branding import synthesize_branding_segment

    # Create mock KokoroClient implementing synthesize_chunk
    mock_kokoro = MagicMock()
    # Fake minimal valid WAV header bytes (44 bytes)
    import struct
    sample_rate = 24000
    num_samples = 24000  # 1 second of audio
    byte_rate = sample_rate * 2
    wav_header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + num_samples * 2,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # Mono
        sample_rate,
        byte_rate,
        2,  # block align
        16, # bits per sample
        b"data",
        num_samples * 2,
    )
    fake_wav_data = wav_header + b"\x00" * (num_samples * 2)

    def fake_synthesize_chunk(text, output_path, voice=None, speed=None, timeout=None):
        output_path.write_bytes(fake_wav_data)
        return output_path

    mock_kokoro.synthesize_chunk.side_effect = fake_synthesize_chunk

    out_file = tmp_path / "test_branding_intro.wav"

    res = synthesize_branding_segment(
        text="This is Herald.",
        output_wav_path=out_file,
        kokoro_client=mock_kokoro,
        voice="af_heart",
        speed=1.0,
        segment_name="intro branding test",
    )

    # Verify synthesize_chunk was called, not synthesize_wav
    mock_kokoro.synthesize_chunk.assert_called_once_with(
        text="This is Herald.",
        output_path=out_file,
        voice="af_heart",
        speed=1.0,
        timeout=None,
    )
    assert out_file.exists()
    assert res["duration_seconds"] > 0

    # Verify restart-safe caching check: second call should reuse cached file without calling client again
    mock_kokoro.reset_mock()
    res2 = synthesize_branding_segment(
        text="This is Herald.",
        output_wav_path=out_file,
        kokoro_client=mock_kokoro,
        voice="af_heart",
        speed=1.0,
        segment_name="intro branding test",
    )
    mock_kokoro.synthesize_chunk.assert_not_called()
    assert res2["path"] == out_file
    assert res2["duration_seconds"] > 0

