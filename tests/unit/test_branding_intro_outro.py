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


def test_intro_template_modes_and_no_duration():
    # Research topic with depth
    intro_res = render_intro_narration("The Birth of the Nuclear Submarine", content_mode="topic", research_depth="medium")
    assert "Herald presents: The Birth of the Nuclear Submarine." in intro_res
    assert "This episode was generated from a research topic using medium research depth." in intro_res
    assert "running approximately" not in intro_res
    assert intro_res.endswith("Let's begin.")

    # Submitted source without publisher
    intro_src = render_intro_narration("The Most Distant Galaxy Yet", content_mode="source")
    assert "Herald presents: The Most Distant Galaxy Yet." in intro_src
    assert "This episode was generated from a submitted source." in intro_src
    assert "running approximately" not in intro_src
    assert intro_src.endswith("Let's begin.")

    # Literal mode
    intro_lit = render_intro_narration("Deep Sea Exploration", content_mode="literal")
    assert "Herald presents: Deep Sea Exploration." in intro_lit
    assert "This episode was generated directly from a submitted source." in intro_lit
    assert "running approximately" not in intro_lit

    # Expanded source with research depth
    intro_exp = render_intro_narration("Quantum Advantage", content_mode="expanded", research_depth="high")
    assert "Herald presents: Quantum Advantage." in intro_exp
    assert "This episode was generated from a submitted source with high research." in intro_exp
    assert "running approximately" not in intro_exp


def test_intro_with_publisher_conditional():
    # When publisher is provided
    intro_pub = render_intro_narration(
        episode_title="The Most Distant Galaxy Yet",
        publisher="BBC Sky at Night Magazine",
        content_mode="source",
    )
    assert intro_pub == "Herald presents: The Most Distant Galaxy Yet. This episode was generated from a submitted source. Based on reporting from BBC Sky at Night Magazine. Let's begin."

    # When publisher is absent
    intro_no_pub = render_intro_narration(
        episode_title="The Most Distant Galaxy Yet",
        publisher=None,
        content_mode="source",
    )
    assert intro_no_pub == "Herald presents: The Most Distant Galaxy Yet. This episode was generated from a submitted source. Let's begin."
    assert "Based on reporting" not in intro_no_pub
    assert "at." not in intro_no_pub


def test_intro_prevents_malformed_headline_and_dangling_artifacts():
    raw_headline = "The James Webb Space Telescope has found the most distant galaxy yet"
    intro = render_intro_narration(
        source_title=raw_headline,
        publisher="at.",  # Dangling word artifact from old bugs
        content_mode="source",
    )
    assert "Based on reporting from at" not in intro
    assert "at. Enjoy." not in intro
    assert intro == f"Herald presents: {sanitize_branding_topic(raw_headline)}. This episode was generated from a submitted source. Let's begin."


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


def test_no_duration_in_intro_even_when_underfilled():
    # User requested 45 minutes, but actual body was only 320 seconds (~5 minutes)
    # New policy: Never announce estimated runtime in intro
    intro_underfilled = render_intro_narration(
        topic="Solid State Batteries",
        target_minutes="45",
        actual_body_duration_seconds=320.0,
    )
    assert "running approximately" not in intro_underfilled
    assert "minutes" not in intro_underfilled
    assert "Herald presents: Solid State Batteries." in intro_underfilled

    # If actual body matches target
    intro_matched = render_intro_narration(
        topic="Solid State Batteries",
        target_minutes="45",
        actual_body_duration_seconds=2670.0,
    )
    assert "running approximately" not in intro_matched
    assert "Herald presents: Solid State Batteries." in intro_matched


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
