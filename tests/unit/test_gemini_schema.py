import pytest
from pydantic import ValidationError

from packages.herald.gemini.schema import PodcastScriptResponse


def test_valid_podcast_script_response():
    data = {
        "episode_title": "AI in 2026",
        "episode_description": "An overview of recent AI developments.",
        "source_title": "Tech News Today",
        "source_url": "https://example.com/article",
        "requested_mode": "standard",
        "segments": [
            {"sequence": 1, "speaker": "host", "text": "Welcome to today's breakdown."},
            {"sequence": 2, "speaker": "host", "text": "Here are the key announcements."},
        ],
    }
    script = PodcastScriptResponse(**data)
    assert script.episode_title == "AI in 2026"
    assert len(script.segments) == 2


def test_invalid_empty_title():
    data = {
        "episode_title": "   ",
        "episode_description": "Valid description",
        "requested_mode": "brief",
        "segments": [{"sequence": 1, "speaker": "host", "text": "Valid text"}],
    }
    with pytest.raises(ValidationError):
        PodcastScriptResponse(**data)


def test_invalid_empty_segment_list():
    data = {
        "episode_title": "Title",
        "episode_description": "Valid description",
        "requested_mode": "brief",
        "segments": [],
    }
    with pytest.raises(ValidationError):
        PodcastScriptResponse(**data)


def test_invalid_segment_sequence_order():
    data = {
        "episode_title": "Title",
        "episode_description": "Valid description",
        "requested_mode": "brief",
        "segments": [
            {"sequence": 1, "speaker": "host", "text": "First segment"},
            {"sequence": 3, "speaker": "host", "text": "Out of order sequence"},
        ],
    }
    with pytest.raises(ValidationError):
        PodcastScriptResponse(**data)
