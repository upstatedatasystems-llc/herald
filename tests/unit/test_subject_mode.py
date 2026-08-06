from packages.herald.db.models import RequestMode
from packages.herald.extraction.email_parser import parse_subject_mode


def test_parse_subject_mode_brief():
    assert parse_subject_mode("Podcast: Brief") == RequestMode.BRIEF
    assert parse_subject_mode("PODCAST:BRIEF") == RequestMode.BRIEF
    assert parse_subject_mode(" Re: podcast: brief ") == RequestMode.BRIEF


def test_parse_subject_mode_standard():
    assert parse_subject_mode("Podcast: Standard") == RequestMode.STANDARD
    assert parse_subject_mode("Fwd: podcast : standard article") == RequestMode.STANDARD


def test_parse_subject_mode_detailed():
    assert parse_subject_mode("Podcast: Detailed") == RequestMode.DETAILED
    assert parse_subject_mode("PODCAST: DETAILED") == RequestMode.DETAILED


def test_parse_subject_mode_fallback():
    assert parse_subject_mode("Random Email Subject") is None
    assert parse_subject_mode("") is None
