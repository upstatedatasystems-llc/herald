import pytest
from pydantic import ValidationError

from herald.gemini.schema import PodcastScriptResponse, PodcastSegment


def test_strict_extra_field_rejected():
    data = {
        "episode_title": "Strict Title",
        "episode_description": "Strict Desc",
        "estimated_minutes": 5,
        "segments": [{"order": 1, "heading": "Intro", "narration": "Hello."}],
        "warnings": [],
        "extra_field": "Not allowed by Appendix C",
    }
    with pytest.raises(ValidationError):
        PodcastScriptResponse(**data)


def test_strict_segment_extra_field_rejected():
    data = {
        "episode_title": "Strict Title",
        "episode_description": "Strict Desc",
        "estimated_minutes": 5,
        "segments": [{"order": 1, "heading": "Intro", "narration": "Hello.", "speaker": "Host"}],
        "warnings": [],
    }
    with pytest.raises(ValidationError):
        PodcastScriptResponse(**data)


def test_missing_warnings_rejected():
    data = {
        "episode_title": "Title",
        "episode_description": "Desc",
        "estimated_minutes": 5,
        "segments": [{"order": 1, "heading": "Intro", "narration": "Hello."}],
    }
    with pytest.raises(ValidationError):
        PodcastScriptResponse(**data)


def test_missing_heading_rejected():
    data = {
        "order": 1,
        "narration": "Hello world",
    }
    with pytest.raises(ValidationError):
        PodcastSegment(**data)


def test_duplicate_order_rejected():
    data = {
        "episode_title": "Title",
        "episode_description": "Desc",
        "estimated_minutes": 5,
        "segments": [
            {"order": 1, "heading": "Intro", "narration": "Hello."},
            {"order": 1, "heading": "Main", "narration": "World."},
        ],
        "warnings": [],
    }
    with pytest.raises(ValidationError):
        PodcastScriptResponse(**data)


def test_zero_estimated_minutes_rejected():
    data = {
        "episode_title": "Title",
        "episode_description": "Desc",
        "estimated_minutes": 0,
        "segments": [{"order": 1, "heading": "Intro", "narration": "Hello."}],
        "warnings": [],
    }
    with pytest.raises(ValidationError):
        PodcastScriptResponse(**data)
