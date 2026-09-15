"""Semantic TTS Chunker for Herald.

Splits canonical podcast script segments into safe TTS chunks bounded by max_chars,
applying deterministic spoken-text normalization while preserving canonical text,
assigning semantic boundary types, and preventing invalid splits within protected tokens.
"""

from __future__ import annotations

import re
from typing import Any

from herald.audio.pause_policy import PAUSE_BY_BOUNDARY, BoundaryType
from herald.tts.lexicon import PronunciationLexicon
from herald.tts.normalizer import TransformationRecord, normalize_for_speech


class TTSChunk:
    """Represents a chunk prepared for Kokoro TTS synthesis."""

    def __init__(
        self,
        index: int,
        text: str,
        segment_order: int = 1,
        is_section_end: bool = False,
        boundary_type: BoundaryType | str = BoundaryType.SENTENCE,
        canonical_text: str | None = None,
        pause_duration_seconds: float | None = None,
        transformations: list[dict[str, str]] | None = None,
    ):
        self.index = index
        self.text = text  # Spoken narration sent to Kokoro
        self.segment_order = segment_order
        self.is_section_end = is_section_end

        if isinstance(boundary_type, str):
            try:
                self.boundary_type = BoundaryType(boundary_type)
            except ValueError:
                self.boundary_type = BoundaryType.SENTENCE
        else:
            self.boundary_type = boundary_type

        self.canonical_text = canonical_text if canonical_text is not None else text
        if pause_duration_seconds is not None:
            self.pause_duration_seconds = float(pause_duration_seconds)
        else:
            self.pause_duration_seconds = PAUSE_BY_BOUNDARY.get(self.boundary_type, 0.5)

        self.transformations = transformations or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "canonical_text": self.canonical_text,
            "segment_order": self.segment_order,
            "is_section_end": self.is_section_end,
            "boundary_type": self.boundary_type.value,
            "pause_duration_seconds": self.pause_duration_seconds,
            "transformations": self.transformations,
        }


def split_text_into_sentences(text: str) -> list[str]:
    """Split text into sentences preserving sentence-ending punctuation.

    Avoids splitting on common abbreviations, decimal numbers, and URLs.
    """
    if not text:
        return []

    # Protect abbreviations with dots
    protected = re.sub(
        r"\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|e\.g|i\.e|Inc|Ltd|Co|Jan|Feb|Mar|Apr|Aug|Sept|Oct|Nov|Dec)\.",
        r"\1<DOT>",
        text,
    )
    # Protect decimal numbers (e.g. 1.5 -> 1<DOT>5)
    protected = re.sub(r"(\d+)\.(\d+)", r"\1<DOT>\2", protected)

    # Split on sentence boundaries: followed by whitespace or end of text
    raw_sentences = re.split(r"(?<=[.!?])\s+", protected)

    clean_sentences = []
    for s in raw_sentences:
        restored = s.replace("<DOT>", ".").strip()
        if restored:
            clean_sentences.append(restored)

    return clean_sentences


def _split_long_sentence(
    canonical_sentence: str,
    spoken_sentence: str,
    max_chars: int,
    transformations: list[TransformationRecord],
) -> list[dict[str, Any]]:
    """Safely split an oversized spoken sentence into chunks <= max_chars.

    Prefers splitting on clause punctuation (semicolon, comma, dash),
    falling back to whitespace, and marking intra-sentence splits as TECHNICAL_SPLIT.
    """
    if len(spoken_sentence) <= max_chars:
        return [{
            "spoken": spoken_sentence,
            "canonical": canonical_sentence,
            "boundary_type": BoundaryType.SENTENCE,
            "transformations": [t.to_dict() for t in transformations],
        }]

    # Try clause splitting first: split on semicolons or commas followed by space
    clauses = re.split(r"(?<=[;:,])\s+", spoken_sentence)
    sub_chunks: list[dict[str, Any]] = []
    current_clause_parts: list[str] = []
    current_len = 0

    for clause in clauses:
        if current_len + len(clause) + 1 <= max_chars:
            current_clause_parts.append(clause)
            current_len += len(clause) + 1
        else:
            if current_clause_parts:
                sub_chunks.append({
                    "spoken": " ".join(current_clause_parts),
                    "canonical": canonical_sentence,
                    "boundary_type": BoundaryType.TECHNICAL_SPLIT,
                    "transformations": [t.to_dict() for t in transformations],
                })
                current_clause_parts = []
                current_len = 0

            if len(clause) > max_chars:
                # Word-level fallback
                words = clause.split(" ")
                current_words: list[str] = []
                word_len = 0
                for w in words:
                    # Pathological single word > max_chars
                    if len(w) > max_chars:
                        if current_words:
                            sub_chunks.append({
                                "spoken": " ".join(current_words),
                                "canonical": canonical_sentence,
                                "boundary_type": BoundaryType.TECHNICAL_SPLIT,
                                "transformations": [t.to_dict() for t in transformations],
                            })
                            current_words = []
                            word_len = 0
                        for part in [w[j : j + max_chars] for j in range(0, len(w), max_chars)]:
                            sub_chunks.append({
                                "spoken": part,
                                "canonical": canonical_sentence,
                                "boundary_type": BoundaryType.TECHNICAL_SPLIT,
                                "transformations": [t.to_dict() for t in transformations],
                            })
                        continue

                    if word_len + len(w) + 1 <= max_chars:
                        current_words.append(w)
                        word_len += len(w) + 1
                    else:
                        if current_words:
                            sub_chunks.append({
                                "spoken": " ".join(current_words),
                                "canonical": canonical_sentence,
                                "boundary_type": BoundaryType.TECHNICAL_SPLIT,
                                "transformations": [t.to_dict() for t in transformations],
                            })
                        current_words = [w]
                        word_len = len(w)
                if current_words:
                    current_clause_parts = current_words
                    current_len = word_len
            else:
                current_clause_parts = [clause]
                current_len = len(clause)

    if current_clause_parts:
        sub_chunks.append({
            "spoken": " ".join(current_clause_parts),
            "canonical": canonical_sentence,
            "boundary_type": BoundaryType.SENTENCE,
            "transformations": [t.to_dict() for t in transformations],
        })

    # The last piece inherits sentence boundary if not already section end
    if sub_chunks:
        sub_chunks[-1]["boundary_type"] = BoundaryType.SENTENCE

    return sub_chunks


def chunk_podcast_script(
    script_segments: list[dict],
    max_chars: int = 500,
    lexicon: PronunciationLexicon | None = None,
) -> list[TTSChunk]:
    """Chunk podcast script segments into safe TTS chunks under max_chars limit.

    Normalizes text for speech deterministically, preserves paragraph and section boundaries,
    marks forced technical splits as TECHNICAL_SPLIT, and sets boundary-aware pause timings.
    """
    chunks: list[TTSChunk] = []
    chunk_index = 0
    total_segments = len(script_segments)

    for i, seg in enumerate(script_segments):
        seg_order = seg.get("order", i + 1)
        raw_narration = seg.get("narration", seg.get("text", "")).strip()
        is_last_segment = (i == total_segments - 1)

        if not raw_narration:
            continue

        # Split into paragraphs to preserve narrative cadence
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\r\n\s*\r\n", raw_narration) if p.strip()]
        if not paragraphs:
            paragraphs = [raw_narration]

        total_paras = len(paragraphs)

        for p_idx, para in enumerate(paragraphs):
            is_last_para = (p_idx == total_paras - 1)
            sentences = split_text_into_sentences(para)
            current_spoken_sentences: list[str] = []
            current_canonical_sentences: list[str] = []
            current_transformations: list[dict[str, str]] = []
            current_len = 0

            for s_idx, sentence in enumerate(sentences):
                is_last_sentence_in_para = (s_idx == len(sentences) - 1)

                # Normalize single sentence
                norm_res = normalize_for_speech(sentence, lexicon=lexicon)
                spoken = norm_res.spoken_text
                trans = [t.to_dict() for t in norm_res.transformations]

                # Check if sentence itself exceeds max_chars
                if len(spoken) > max_chars:
                    # Flush accumulated sentences first
                    if current_spoken_sentences:
                        chunk_index += 1
                        chunks.append(
                            TTSChunk(
                                index=chunk_index,
                                text=" ".join(current_spoken_sentences),
                                canonical_text=" ".join(current_canonical_sentences),
                                segment_order=seg_order,
                                is_section_end=False,
                                boundary_type=BoundaryType.SENTENCE,
                                transformations=current_transformations,
                            )
                        )
                        current_spoken_sentences = []
                        current_canonical_sentences = []
                        current_transformations = []
                        current_len = 0

                    # Split the oversized sentence safely
                    sub_parts = _split_long_sentence(
                        canonical_sentence=sentence,
                        spoken_sentence=spoken,
                        max_chars=max_chars,
                        transformations=norm_res.transformations,
                    )
                    for sub_idx, sub in enumerate(sub_parts):
                        chunk_index += 1
                        is_final_sub = (sub_idx == len(sub_parts) - 1)
                        if is_final_sub and is_last_sentence_in_para:
                            if is_last_para:
                                b_type = BoundaryType.SECTION if not is_last_segment else BoundaryType.SECTION
                                sec_end = not is_last_segment
                            else:
                                b_type = BoundaryType.PARAGRAPH
                                sec_end = False
                        else:
                            b_type = sub["boundary_type"]
                            sec_end = False

                        chunks.append(
                            TTSChunk(
                                index=chunk_index,
                                text=sub["spoken"],
                                canonical_text=sub["canonical"],
                                segment_order=seg_order,
                                is_section_end=sec_end,
                                boundary_type=b_type,
                                transformations=sub["transformations"],
                            )
                        )
                    continue

                sentence_len = len(spoken)
                if current_len + sentence_len + 1 <= max_chars:
                    current_spoken_sentences.append(spoken)
                    current_canonical_sentences.append(sentence)
                    current_transformations.extend(trans)
                    current_len += sentence_len + 1
                else:
                    # Flush accumulated chunk
                    chunk_index += 1
                    chunks.append(
                        TTSChunk(
                            index=chunk_index,
                            text=" ".join(current_spoken_sentences),
                            canonical_text=" ".join(current_canonical_sentences),
                            segment_order=seg_order,
                            is_section_end=False,
                            boundary_type=BoundaryType.SENTENCE,
                            transformations=current_transformations,
                        )
                    )
                    current_spoken_sentences = [spoken]
                    current_canonical_sentences = [sentence]
                    current_transformations = list(trans)
                    current_len = sentence_len

            if current_spoken_sentences:
                chunk_index += 1
                if is_last_para:
                    b_type = BoundaryType.SECTION
                    sec_end = not is_last_segment
                else:
                    b_type = BoundaryType.PARAGRAPH
                    sec_end = False

                chunks.append(
                    TTSChunk(
                        index=chunk_index,
                        text=" ".join(current_spoken_sentences),
                        canonical_text=" ".join(current_canonical_sentences),
                        segment_order=seg_order,
                        is_section_end=sec_end,
                        boundary_type=b_type,
                        transformations=current_transformations,
                    )
                )

    return chunks
