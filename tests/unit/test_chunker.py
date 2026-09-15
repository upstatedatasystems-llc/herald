"""Unit tests for Semantic TTS Chunker.

Tests:
1. Sentence splitting with protected abbreviations & decimal numbers.
2. Max char bounds enforcement.
3. Semantic boundary classification (SENTENCE, PARAGRAPH, SECTION, TECHNICAL_SPLIT).
4. Boundary-aware pause assignment (0.0s for technical split, 0.5s sentence, 0.8s paragraph, 1.2s section).
5. Spoken normalization derivation without altering canonical script.
6. Safe splitting of oversized sentences.
"""

from herald.tts.chunker import (
    BoundaryType,
    chunk_podcast_script,
    split_text_into_sentences,
)


def test_split_text_into_sentences_protects_abbreviations():
    text = "Dr. Smith met Mr. Jones vs. the committee. It was a success! How are you?"
    sentences = split_text_into_sentences(text)
    assert len(sentences) == 3
    assert sentences[0] == "Dr. Smith met Mr. Jones vs. the committee."
    assert sentences[1] == "It was a success!"
    assert sentences[2] == "How are you?"


def test_split_text_into_sentences_protects_decimals():
    text = "The score was 9.5 out of 10. That is great!"
    sentences = split_text_into_sentences(text)
    assert len(sentences) == 2
    assert sentences[0] == "The score was 9.5 out of 10."
    assert sentences[1] == "That is great!"


def test_chunk_podcast_script_respects_max_chars():
    segments = [
        {
            "order": 1,
            "narration": "This is paragraph one sentence one. This is paragraph one sentence two.",
        },
        {
            "order": 2,
            "narration": "This is paragraph two sentence one. This is paragraph two sentence two.",
        },
    ]

    chunks = chunk_podcast_script(segments, max_chars=80)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk.text) <= 80


def test_semantic_boundaries_and_pause_durations():
    segments = [
        {
            "order": 1,
            # Two paragraphs separated by double newline
            "narration": "First para sentence one. First para sentence two.\n\nSecond para sentence one.",
        },
        {
            "order": 2,
            "narration": "Third para sentence in section two.",
        },
    ]

    chunks = chunk_podcast_script(segments, max_chars=500)
    assert len(chunks) == 3

    # Chunk 1: End of paragraph 1 in segment 1 -> PARAGRAPH boundary
    c1 = chunks[0]
    assert c1.boundary_type == BoundaryType.PARAGRAPH
    assert c1.pause_duration_seconds == 0.8
    assert not c1.is_section_end

    # Chunk 2: End of segment 1 -> SECTION boundary
    c2 = chunks[1]
    assert c2.boundary_type == BoundaryType.SECTION
    assert c2.pause_duration_seconds == 1.2
    assert c2.is_section_end

    # Chunk 3: End of segment 2 (last segment) -> SECTION boundary
    c3 = chunks[2]
    assert c3.boundary_type == BoundaryType.SECTION
    assert not c3.is_section_end  # Last segment doesn't insert inter-section pause


def test_technical_split_assigns_zero_pause():
    # Long sentence that exceeds max_chars
    long_sentence = (
        "This is an exceptionally detailed sentence about astrophysics and astronomy, "
        "designed specifically to exceed the maximum character limit set for testing, "
        "requiring the chunker to split it across clause boundaries safely."
    )
    segments = [{"order": 1, "narration": long_sentence}]

    chunks = chunk_podcast_script(segments, max_chars=100)
    assert len(chunks) > 1

    # Intra-sentence chunks must be TECHNICAL_SPLIT with 0.0s pause
    technical_splits = [c for c in chunks if c.boundary_type == BoundaryType.TECHNICAL_SPLIT]
    assert len(technical_splits) >= 1
    for tc in technical_splits:
        assert tc.pause_duration_seconds == 0.0


def test_spoken_normalization_applied_with_canonical_preserved():
    segments = [
        {
            "order": 1,
            "narration": "In 1955, the B-52 took flight, and JWST's discoveries reshaped physics.",
        }
    ]

    chunks = chunk_podcast_script(segments, max_chars=500)
    assert len(chunks) == 1
    c = chunks[0]

    # Spoken text is normalized
    assert "nineteen fifty-five" in c.text
    assert "B fifty-two" in c.text
    assert "J W S T's" in c.text

    # Canonical text is preserved
    assert "1955" in c.canonical_text
    assert "B-52" in c.canonical_text
    assert "JWST's" in c.canonical_text
    assert len(c.transformations) > 0
