from herald.db.models import RequestMode
from herald.extraction.email_parser import parse_subject_mode


def test_parse_subject_mode_brief():
    assert parse_subject_mode("Podcast: Brief") == RequestMode.BRIEF
    assert parse_subject_mode("podcast: brief") == RequestMode.BRIEF


def test_parse_subject_mode_standard():
    assert parse_subject_mode("Podcast: Standard") == RequestMode.STANDARD
    assert parse_subject_mode("podcast: standard") == RequestMode.STANDARD


def test_parse_subject_mode_detailed():
    assert parse_subject_mode("Podcast: Detailed") == RequestMode.DETAILED
    assert parse_subject_mode("podcast: detailed") == RequestMode.DETAILED


def test_parse_subject_mode_fallback():
    # Substring matches must return None
    assert parse_subject_mode("Weekly Podcast: Briefing") is None
    assert parse_subject_mode("Notes about Podcast: Standard") is None
