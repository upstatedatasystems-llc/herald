import pytest

from herald.extraction.email_parser import parse_directives


def test_parse_directives_valid():
    raw_body = """Voice: af_bella
Speed: 1.1
Title: Special Episode Headline

Here is the actual article content following the directives."""

    text, voice, speed, title, research_depth = parse_directives(raw_body)
    assert voice == "af_bella"
    assert speed == 1.1
    assert title == "Special Episode Headline"
    assert research_depth is None
    assert "Voice:" not in text
    assert "Speed:" not in text
    assert "Title:" not in text
    assert "Here is the actual article content" in text


def test_parse_directives_out_of_bounds_speed():
    raw_body = """Speed: 2.5
Here is normal body content."""

    with pytest.raises(ValueError):
        parse_directives(raw_body)
