import pytest
from pydantic import ValidationError

from herald.gemini.schema import PodcastScriptResponse


def test_valid_appendix_c_podcast_script_response():
    data = {
        "episode_title": "AI in 2026",
        "episode_description": "An overview of recent AI developments.",
        "estimated_minutes": 10,
        "source_title": "Tech News Today",
        "segments": [
            {"order": 1, "heading": "Introduction", "narration": "Welcome to today's breakdown."},
            {"order": 2, "heading": "Main Developments", "narration": "Here are the key announcements."},
        ],
        "warnings": [],
    }
    script = PodcastScriptResponse(**data)
    assert script.episode_title == "AI in 2026"
    assert script.estimated_minutes == 10
    assert len(script.segments) == 2
    assert script.segments[0].narration == "Welcome to today's breakdown."


def test_invalid_empty_title():
    data = {
        "episode_title": "   ",
        "episode_description": "Valid description",
        "estimated_minutes": 5,
        "segments": [{"order": 1, "heading": "Intro", "narration": "Valid narration"}],
        "warnings": [],
    }
    with pytest.raises(ValidationError):
        PodcastScriptResponse(**data)


def test_invalid_empty_segment_list():
    data = {
        "episode_title": "Title",
        "episode_description": "Valid description",
        "estimated_minutes": 5,
        "segments": [],
        "warnings": [],
    }
    with pytest.raises(ValidationError):
        PodcastScriptResponse(**data)


def test_invalid_segment_sequence_order():
    data = {
        "episode_title": "Title",
        "episode_description": "Valid description",
        "estimated_minutes": 5,
        "segments": [
            {"order": 1, "heading": "Intro", "narration": "First segment"},
            {"order": 3, "heading": "Main", "narration": "Out of order sequence"},
        ],
        "warnings": [],
    }
    with pytest.raises(ValidationError):
        PodcastScriptResponse(**data)
