import pytest

from herald.db.models import RequestMode
from herald.extraction.email_parser import (
    SourceClassification,
    classify_source_content,
    parse_directives,
    parse_subject_mode,
)


def test_exact_subject_matching():
    assert parse_subject_mode("Podcast: Brief") == RequestMode.BRIEF
    assert parse_subject_mode("Fwd: Re: Podcast: Standard") == RequestMode.STANDARD
    assert parse_subject_mode("FW: Podcast: Detailed") == RequestMode.DETAILED

    # Substring matches must be rejected (return None)
    assert parse_subject_mode("Weekly Podcast: Briefing") is None
    assert parse_subject_mode("Notes about Podcast: Standard") is None
    assert parse_subject_mode("Podcast: Detailed later") is None


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
