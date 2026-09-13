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
    outro = render_outro_narration()
    assert outro == OUTRO_TEMPLATE
    assert outro == "You've been listening to Herald, the open-source podcast generation platform."


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
