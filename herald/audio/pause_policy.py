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


# Maximum pause duration caps in seconds
MAX_PAUSE_SENTENCE: float = 0.85
MAX_PAUSE_PARAGRAPH: float = 1.30
MAX_PAUSE_SECTION: float = 1.80


def calculate_complexity_pause(
    text: str,
    boundary: BoundaryType | str,
) -> tuple[float, str]:
    """
    Calculate deterministic complexity-aware pause duration for a chunk or boundary.
    Returns (pause_duration_seconds, reason_description).
    
    Signals:
    - Sentence: baseline 0.5s.
      Bonuses:
      - Long sentence length (>=28 words: +0.10s, >=38 words: +0.15s)
      - Multiple clauses (>=2 internal punctuation marks: +0.05s, >=3: +0.10s)
      - Acronyms / identifiers (>=2: +0.05s)
      - Numbers / measurements (>=2: +0.05s)
      Cap: MAX_PAUSE_SENTENCE (0.85s)
    - Paragraph: baseline 0.8s.
      Bonuses:
      - Dense paragraph (>=55 words: +0.15s, >=80 words: +0.25s)
      - Technical terms/numbers density (+0.05s)
      Cap: MAX_PAUSE_PARAGRAPH (1.30s)
    - Section: baseline 1.2s + modest transition separation (+0.20s = 1.40s).
      Cap: MAX_PAUSE_SECTION (1.80s)
    - Technical split: 0.0s
    - Branding: 1.2s
    """
    import re

    b_type = boundary if isinstance(boundary, BoundaryType) else None
    if b_type is None:
        try:
            b_type = BoundaryType(str(boundary))
        except ValueError:
            b_type = BoundaryType.SENTENCE

    if b_type == BoundaryType.TECHNICAL_SPLIT:
        return PAUSE_TECHNICAL_SPLIT, "technical_split (0.0s)"

    if b_type == BoundaryType.BRANDING:
        return PAUSE_BRANDING, "branding (1.2s)"

    words = text.split()
    word_count = len(words)

    if b_type == BoundaryType.SECTION:
        base = PAUSE_SECTION
        bonus = 0.0
        reasons = []
        if word_count >= 50:
            bonus += 0.30
            reasons.append("dense_section_end (+0.30s)")
        elif word_count >= 25 or len(re.findall(r"[,;:—–-]", text)) >= 2:
            bonus += 0.20
            reasons.append("section_transition (+0.20s)")

        total_pause = min(MAX_PAUSE_SECTION, base + bonus)
        reason_str = ", ".join(reasons) if reasons else "baseline"
        return total_pause, f"section_boundary ({reason_str}) -> {total_pause:.2f}s"

    if b_type == BoundaryType.PARAGRAPH:
        base = PAUSE_PARAGRAPH
        bonus = 0.0
        reasons = []
        if word_count >= 80:
            bonus += 0.25
            reasons.append(f"long_paragraph ({word_count}w, +0.25s)")
        elif word_count >= 55:
            bonus += 0.15
            reasons.append(f"dense_paragraph ({word_count}w, +0.15s)")

        # Multiple numbers or acronyms
        num_count = len(re.findall(r"\b\d+(?:[.,]\d+)*\b", text))
        acronym_count = len(re.findall(r"\b[A-Z]{2,}\b", text))
        if num_count + acronym_count >= 3:
            bonus += 0.05
            reasons.append(f"technical_density ({num_count} nums, {acronym_count} acronyms, +0.05s)")

        total_pause = min(MAX_PAUSE_PARAGRAPH, base + bonus)
        reason_str = ", ".join(reasons) if reasons else "baseline"
        return total_pause, f"paragraph ({reason_str}) -> {total_pause:.2f}s"

    # Default: BoundaryType.SENTENCE
    base = PAUSE_SENTENCE
    bonus = 0.0
    reasons = []

    if word_count >= 38:
        bonus += 0.15
        reasons.append(f"very_long_sentence ({word_count}w, +0.15s)")
    elif word_count >= 28:
        bonus += 0.10
        reasons.append(f"long_sentence ({word_count}w, +0.10s)")

    # Multiple clauses (commas, semicolons, colons, dashes)
    clause_marks = len(re.findall(r"[,;:—–-]", text))
    if clause_marks >= 3:
        bonus += 0.10
        reasons.append(f"multi_clause ({clause_marks} marks, +0.10s)")
    elif clause_marks >= 2:
        bonus += 0.05
        reasons.append(f"multi_clause ({clause_marks} marks, +0.05s)")

    # Acronyms / identifiers / models
    acronyms = len(re.findall(r"\b[A-Z]{2,}\b|\b[A-Za-z]+-\d+\b", text))
    if acronyms >= 2:
        bonus += 0.05
        reasons.append(f"acronyms_identifiers ({acronyms}, +0.05s)")

    # Numbers or measurements
    nums = len(re.findall(r"\b\d+(?:[.,]\d+)*\b", text))
    if nums >= 2:
        bonus += 0.05
        reasons.append(f"numbers_measurements ({nums}, +0.05s)")

    total_pause = min(MAX_PAUSE_SENTENCE, base + bonus)
    reason_str = ", ".join(reasons) if reasons else "baseline"
    return total_pause, f"sentence ({reason_str}) -> {total_pause:.2f}s"

