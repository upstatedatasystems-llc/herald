import pytest

from herald.literal.script_generator import generate_literal_script
from herald.telegram.bot import parse_telegram_message_directives


def test_forwarded_newsletter_with_links_remains_text():
    """Forwarded newsletter containing multiple links is treated as text input."""
    msg = """
    Weekly Engineering Briefing

    Here are the top articles this week:
    1. Distributed consensus: https://example.com/consensus
    2. Zero knowledge proofs: https://example.com/zkp

    In our main segment, we analyze recent advancements in compiler toolchains.
    """
    res = parse_telegram_message_directives(msg)
    assert res["url"] is None
    assert "Weekly Engineering Briefing" in res["text"]
    assert "Distributed consensus" in res["text"]


def test_single_standalone_url_becomes_url_input():
    """A standalone article URL with optional mode header is parsed as URL input."""
    msg = """
    literal
    Voice: af_bella
    https://example.com/single-deep-dive-article
    """
    res = parse_telegram_message_directives(msg)
    assert res["url"] == "https://example.com/single-deep-dive-article"
    assert res["mode"] == "literal"
    assert res["voice"] == "af_bella"


def test_directive_parsing_stops_at_first_non_directive_line():
    """Directive parsing stops permanently once body text begins."""
    msg = """
    standard
    Title: Cloud Infrastructure

    Paragraph 1: Introduction to cloud architecture.

    Standard practices must be followed.

    Title: This internal heading should not change episode title.
    """
    res = parse_telegram_message_directives(msg)
    assert res["mode"] == "standard"
    assert res["title"] == "Cloud Infrastructure"
    assert "Standard practices must be followed." in res["text"]
    assert "Title: This internal heading" in res["text"]


def test_paragraph_structure_and_blank_lines_survive_parsing():
    """Paragraphs and formatting survive parsing."""
    msg = "literal\n\nParagraph 1 is here.\n\nParagraph 2 is here.\n\nParagraph 3 is here."
    res = parse_telegram_message_directives(msg)
    assert res["mode"] == "literal"
    assert "Paragraph 1 is here.\n\nParagraph 2 is here.\n\nParagraph 3 is here." in res["text"]


def test_invalid_chunk_directive_rejected():
    """Invalid chunk size raises clear ValueError."""
    msg = "chunk-50\nhttps://example.com/art"
    with pytest.raises(ValueError, match="Invalid chunk size"):
        parse_telegram_message_directives(msg)


def test_literal_mode_preserves_short_first_line_and_all_sentences():
    """Literal mode preserves short first lines without dropping them as title."""
    source = "Autonomous driving.\n\nVision systems and radar sensors operate collaboratively."
    script = generate_literal_script(source)
    assert len(script.segments) >= 1
    # First sentence must be part of narration
    full_narration = " ".join(seg.narration for seg in script.segments)
    assert "Autonomous driving" in full_narration
    assert "Vision systems and radar sensors operate collaboratively" in full_narration
