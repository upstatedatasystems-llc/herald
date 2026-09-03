import pytest

from herald.db.models import RequestMode
from herald.extraction.email_parser import (
    SourceClassification,
    classify_source_content,
    parse_directives,
    parse_subject_directives,
    parse_subject_mode,
)


def test_exact_subject_matching():
    assert parse_subject_mode("Podcast: Brief") == RequestMode.BRIEF
    assert parse_subject_mode("Fwd: Re: Podcast: Standard") == RequestMode.STANDARD
    assert parse_subject_mode("FW: Podcast: Detailed") == RequestMode.RESEARCH

    # Substring matches must be rejected (return None)
    assert parse_subject_mode("Weekly Podcast: Briefing") is None
    assert parse_subject_mode("Notes about Podcast: Standard") is None
    assert parse_subject_mode("Podcast: Detailed later") is None


def test_parse_subject_directives_extended():
    mode1, depth1, chunk1, verify1 = parse_subject_directives("Podcast: Brief")
    assert mode1 == RequestMode.BRIEF
    assert depth1 is None
    assert chunk1 == 500
    assert verify1 is False

    mode2, depth2, chunk2, verify2 = parse_subject_directives("Podcast: Standard chunk-500")
    assert mode2 == RequestMode.STANDARD
    assert chunk2 == 500
    assert verify2 is False

    mode3, depth3, chunk3, verify3 = parse_subject_directives("Podcast: Standard verify")
    assert mode3 == RequestMode.STANDARD
    assert chunk3 == 500
    assert verify3 is True

    mode4, depth4, chunk4, verify4 = parse_subject_directives("Podcast: Standard verify chunk-1000")
    assert mode4 == RequestMode.STANDARD
    assert chunk4 == 1000
    assert verify4 is True

    mode5, depth5, chunk5, verify5 = parse_subject_directives("Podcast: Research High verify chunk-1000")
    assert mode5 == RequestMode.RESEARCH
    assert depth5 == "high"
    assert chunk5 == 1000
    assert verify5 is True

    mode6, depth6, chunk6, verify6 = parse_subject_directives("Podcast: Research chunk-1000 verify")
    assert mode6 == RequestMode.RESEARCH
    assert depth6 is None
    assert chunk6 == 1000
    assert verify6 is True

    mode7, depth7, chunk7, verify7 = parse_subject_directives("Podcast: Standard CHUNK-1000 VERIFY")
    assert mode7 == RequestMode.STANDARD
    assert chunk7 == 1000
    assert verify7 is True

    mode8, depth8, chunk8, verify8 = parse_subject_directives("Podcast: Standard verify verify")
    assert mode8 == RequestMode.STANDARD
    assert verify8 is True

    mode9, depth9, chunk9, verify9 = parse_subject_directives("Podcast: Standard chunk-250")
    assert chunk9 == 250

    # Conflicting duplicate chunk directives must be rejected
    with pytest.raises(ValueError, match="Conflicting or duplicate chunk command"):
        parse_subject_directives("Podcast: Standard chunk-500 chunk-1000")

    # Below minimum (250) must be rejected
    with pytest.raises(ValueError, match="Chunk size must be between"):
        parse_subject_directives("Podcast: Standard chunk-100")

    # Above maximum (1000) must be rejected
    with pytest.raises(ValueError, match="Chunk size must be between"):
        parse_subject_directives("Podcast: Standard chunk-5000")

    # Malformed chunk directive must be rejected
    with pytest.raises(ValueError, match="Malformed chunk command"):
        parse_subject_directives("Podcast: Standard chunk-abc")


def test_invalid_directive_rejected():
    with pytest.raises(ValueError):
        parse_directives("Voice: invalid_unknown_voice\nBody content...")

    with pytest.raises(ValueError):
        parse_directives("Speed: 3.5\nBody content...")


def test_source_classification():
    # Bare URL -> url mode
    c1, url1 = classify_source_content("https://example.com/article", ["https://example.com/article"])
    assert c1 == SourceClassification.URL
    assert url1 == "https://example.com/article"

    # Article label + URL -> url mode
    c2, url2 = classify_source_content("Article: https://example.com/article", ["https://example.com/article"])
    assert c2 == SourceClassification.URL

    # Commentary + URL -> email_body mode
    c3, url3 = classify_source_content("Please summarize this critically:\nhttps://example.com/article", ["https://example.com/article"])
    assert c3 == SourceClassification.EMAIL_BODY
    assert url3 is None

    # Multiple bare URLs -> invalid_multiple_urls mode
    c4, url4 = classify_source_content("https://example.com/a\nhttps://example.com/b", ["https://example.com/a", "https://example.com/b"])
    assert c4 == SourceClassification.INVALID_MULTIPLE_URLS
