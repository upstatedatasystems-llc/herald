"""Unit tests for Deterministic Herald Intro/Outro Branding.

Tests:
- Controlled conditional templates (with and without publisher)
- Episode title and source headline separation
- Fixed duration and auto/literal templates
- Truthful duration reporting when underfilled
- Outro template immutability
- Topic and publisher sanitization edge cases
- Prevention of dangling "at. Enjoy." artifacts
- Branding segments are application-owned and not from LLM
"""

import struct
from unittest.mock import MagicMock

from herald.audio.branding import (
    OUTRO_TEMPLATE,
    render_intro_narration,
    render_outro_narration,
    sanitize_branding_topic,
    sanitize_publisher_name,
    synthesize_branding_segment,
)


def test_intro_template_fixed_duration():
    intro_10 = render_intro_narration("Virginia-class submarines", target_minutes="10")
    assert "Today's episode is 'Virginia-class submarines'" in intro_10
    assert "running approximately 10 minutes" in intro_10
    assert intro_10.startswith("This is Herald.")
    assert intro_10.endswith("Let's begin.")

    intro_45 = render_intro_narration("Quantum Computing", target_minutes=45)
    assert "Today's episode is 'Quantum Computing'" in intro_45
    assert "running approximately 45 minutes" in intro_45


def test_intro_template_auto_and_literal():
    intro_auto = render_intro_narration("James Webb Space Telescope", target_minutes="auto")
    assert "Today's episode is 'James Webb Space Telescope'" in intro_auto
    assert "running approximately" not in intro_auto
    assert intro_auto.endswith("Let's begin.")

    intro_none = render_intro_narration("Mars Rover", target_minutes=None)
    assert "Today's episode is 'Mars Rover'" in intro_none
    assert "running approximately" not in intro_none

    intro_literal = render_intro_narration("Deep Sea Exploration", target_minutes="10", content_mode="literal")
    assert "Today's episode is 'Deep Sea Exploration'" in intro_literal
    assert "running approximately" not in intro_literal


def test_intro_with_publisher_conditional():
    # When publisher is provided
    intro_pub = render_intro_narration(
        episode_title="The Most Distant Galaxy Yet",
        publisher="BBC Sky at Night Magazine",
    )
    assert intro_pub == "This is Herald. Today's episode is 'The Most Distant Galaxy Yet'. Based on reporting from BBC Sky at Night Magazine. Let's begin."

    # When publisher is absent
    intro_no_pub = render_intro_narration(
        episode_title="The Most Distant Galaxy Yet",
        publisher=None,
    )
    assert intro_no_pub == "This is Herald. Today's episode is 'The Most Distant Galaxy Yet'. Let's begin."
    assert "Based on reporting" not in intro_no_pub
    assert "at." not in intro_no_pub


def test_intro_prevents_malformed_headline_and_dangling_artifacts():
    # Raw headline does not produce broken "podcast about the James Webb Space Telescope has found..."
    raw_headline = "The James Webb Space Telescope has found the most distant galaxy yet"
    intro = render_intro_narration(
        source_title=raw_headline,
        publisher="at.",  # Dangling word artifact from old bugs
    )
    # Dangling publisher stripped cleanly
    assert "Based on reporting from at" not in intro
    assert "at. Enjoy." not in intro
    assert intro == f"This is Herald. Today's episode is '{sanitize_branding_topic(raw_headline)}'. Let's begin."


def test_publisher_sanitization():
    assert sanitize_publisher_name(None) is None
    assert sanitize_publisher_name("") is None
    assert sanitize_publisher_name("   ") is None
    assert sanitize_publisher_name("at.") is None
    assert sanitize_publisher_name("enjoy") is None
    assert sanitize_publisher_name("https://arstechnica.com") == "arstechnica.com"
    assert sanitize_publisher_name("  The Verge!  ") == "The Verge"


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
    url = "https://example.com/2026/09/12/breakthrough-in-fusion-power?utm_source=rss&ref=twitter"
    clean_url_topic = sanitize_branding_topic(url)
    assert clean_url_topic == "Breakthrough In Fusion Power"
    assert "https" not in clean_url_topic
    assert "utm" not in clean_url_topic

    domain_url = "https://arstechnica.com/"
    assert sanitize_branding_topic(domain_url) == "Arstechnica"


def test_truthful_duration_reporting_when_underfilled():
    # User requested 45 minutes, but actual body was only 320 seconds (~5 minutes)
    intro_underfilled = render_intro_narration(
        topic="Solid State Batteries",
        target_minutes="45",
        actual_body_duration_seconds=320.0,
    )
    assert "running approximately 5 minutes" in intro_underfilled
    assert "45" not in intro_underfilled

    # If actual body matches target (~44.5 min = 2670s for 45 min target)
    intro_matched = render_intro_narration(
        topic="Solid State Batteries",
        target_minutes="45",
        actual_body_duration_seconds=2670.0,
    )
    assert "running approximately 45 minutes" in intro_matched


def test_synthesize_branding_segment_contract(tmp_path):
    mock_kokoro = MagicMock()
    sample_rate = 24000
    num_samples = 24000
    byte_rate = sample_rate * 2
    wav_header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + num_samples * 2,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        byte_rate,
        2,
        16,
        b"data",
        num_samples * 2,
    )
    fake_wav_data = wav_header + b"\x00" * (num_samples * 2)

    def fake_synthesize_chunk(text, output_path, voice=None, speed=None, timeout=None):
        output_path.write_bytes(fake_wav_data)
        return output_path

    mock_kokoro.synthesize_chunk.side_effect = fake_synthesize_chunk

    out_file = tmp_path / "test_intro.wav"
    res = synthesize_branding_segment(
        text="This is Herald. Let's begin.",
        output_wav_path=out_file,
        kokoro_client=mock_kokoro,
        voice="af_heart",
        speed=1.0,
    )

    assert res["path"] == out_file
    assert res["duration_seconds"] > 0
    assert out_file.exists()
    assert mock_kokoro.synthesize_chunk.called
