"""
Unit tests verifying AI Schema Neutrality and backward compatibility layer.
"""

from herald.ai.schema import PodcastScriptResponse as NeutralScriptResponse
from herald.ai.schema import PodcastSegment as NeutralSegment
from herald.gemini.schema import PodcastScriptResponse as GeminiScriptResponse
from herald.gemini.schema import PodcastSegment as GeminiSegment


def test_schema_reexport_identity():
    """Verify gemini.schema re-exports the canonical models from herald.ai.schema directly."""
    assert GeminiScriptResponse is NeutralScriptResponse
    assert GeminiSegment is NeutralSegment


def test_neutral_script_validation():
    """Verify neutral script response validates ordered segments and fields correctly."""
    valid_data = {
        "episode_title": "Test Title",
        "episode_description": "Test Description",
        "estimated_minutes": 3,
        "source_title": "Source Article",
        "segments": [
            {"order": 1, "heading": "Introduction", "narration": "First segment content."},
            {"order": 2, "heading": "Deep Dive", "narration": "Second segment content."},
        ],
        "warnings": [],
    }
    script = NeutralScriptResponse(**valid_data)
    assert script.episode_title == "Test Title"
    assert len(script.segments) == 2
    assert script.segments[0].order == 1
    assert script.segments[1].order == 2
