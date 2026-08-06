from packages.herald.extraction.email_parser import parse_directives


def test_parse_directives_valid():
    raw_body = """Voice: af_bella
Speed: 1.1
Title: Special Episode Headline

Here is the actual article content following the directives."""

    text, voice, speed, title = parse_directives(raw_body)
    assert voice == "af_bella"
    assert speed == 1.1
    assert title == "Special Episode Headline"
    assert "Voice:" not in text
    assert "Speed:" not in text
    assert "Title:" not in text
    assert "Here is the actual article content" in text


def test_parse_directives_out_of_bounds_speed():
    raw_body = """Speed: 2.5
Here is normal body content."""

    text, voice, speed, title = parse_directives(raw_body)
    assert speed is None  # Out of bounds speed (0.8 - 1.2) ignored
    assert "Here is normal body content." in text
