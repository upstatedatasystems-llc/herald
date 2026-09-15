"""Authoritative Semantic Pause Policy for Herald Audio and TTS.

Defines the single source of truth for semantic boundary classifications,
associated pause durations (in seconds), and stream padding.
"""

from __future__ import annotations

import enum


class BoundaryType(str, enum.Enum):
    """Semantic boundary classification for audio pacing."""

    TECHNICAL_SPLIT = "TECHNICAL_SPLIT"
    SENTENCE = "SENTENCE"
    PARAGRAPH = "PARAGRAPH"
    SECTION = "SECTION"
    BRANDING = "BRANDING"


# Authoritative baseline pause durations in seconds
PAUSE_TECHNICAL_SPLIT: float = 0.0
PAUSE_SENTENCE: float = 0.5
PAUSE_PARAGRAPH: float = 0.8
PAUSE_SECTION: float = 1.2
PAUSE_BRANDING: float = 1.2

# Stream padding in seconds
PAUSE_PADDING_START: float = 0.8
PAUSE_PADDING_END: float = 0.8

# Authoritative mapping by BoundaryType enum
PAUSE_BY_BOUNDARY: dict[BoundaryType, float] = {
    BoundaryType.TECHNICAL_SPLIT: PAUSE_TECHNICAL_SPLIT,
    BoundaryType.SENTENCE: PAUSE_SENTENCE,
    BoundaryType.PARAGRAPH: PAUSE_PARAGRAPH,
    BoundaryType.SECTION: PAUSE_SECTION,
    BoundaryType.BRANDING: PAUSE_BRANDING,
}

# String lookup mapping for serialization / FFmpeg boundary types
PAUSE_DURATION_BY_BOUNDARY: dict[str, float] = {
    k.value: v for k, v in PAUSE_BY_BOUNDARY.items()
}


def get_pause_duration(boundary: BoundaryType | str) -> float:
    """Return authoritative pause duration in seconds for a boundary type."""
    if isinstance(boundary, BoundaryType):
        return PAUSE_BY_BOUNDARY.get(boundary, PAUSE_SENTENCE)
    try:
        b_type = BoundaryType(str(boundary))
        return PAUSE_BY_BOUNDARY.get(b_type, PAUSE_SENTENCE)
    except ValueError:
        return PAUSE_DURATION_BY_BOUNDARY.get(str(boundary), PAUSE_SENTENCE)
