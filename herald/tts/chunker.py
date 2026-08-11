import re


class TTSChunk:
    def __init__(self, index: int, text: str, segment_order: int = 1, is_section_end: bool = False):
        self.index = index
        self.text = text
        self.segment_order = segment_order
        self.is_section_end = is_section_end



def split_text_into_sentences(text: str) -> list[str]:
    """
    Split text into sentences preserving sentence-ending punctuation.
    Avoids splitting on common abbreviations.
    """
    if not text:
        return []

    protected = re.sub(r"\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|e\.g|i\.e|Inc|Ltd|Co)\.", r"\1<DOT>", text)
    sentences = re.split(r"(?<=[.!?])\s+", protected)

    clean_sentences = []
    for s in sentences:
        restored = s.replace("<DOT>", ".").strip()
        if restored:
            clean_sentences.append(restored)

    return clean_sentences


def chunk_podcast_script(script_segments: list[dict], max_chars: int = 500) -> list[TTSChunk]:
    """
    Chunk podcast script segments into safe TTS chunks under max_chars limit,
    preserving sentence and paragraph boundaries, enforcing strict max_chars even for pathological single tokens.
    """
    chunks: list[TTSChunk] = []
    chunk_index = 0
    total_segments = len(script_segments)

    for i, seg in enumerate(script_segments):
        seg_order = seg.get("order", i + 1)
        text = seg.get("narration", seg.get("text", "")).strip()
        is_last_segment = (i == total_segments - 1)

        if not text:
            continue

        sentences = split_text_into_sentences(text)
        current_chunk_sentences: list[str] = []
        current_len = 0

        for sentence in sentences:
            sentence_len = len(sentence)

            if current_len + sentence_len + 1 <= max_chars:
                current_chunk_sentences.append(sentence)
                current_len += sentence_len + 1
            else:
                if current_chunk_sentences:
                    chunk_index += 1
                    chunks.append(
                        TTSChunk(
                            index=chunk_index,
                            text=" ".join(current_chunk_sentences),
                            segment_order=seg_order,
                            is_section_end=False,
                        )
                    )
                    current_chunk_sentences = []
                    current_len = 0

                if sentence_len > max_chars:
                    words = sentence.split(" ")
                    sub_words: list[str] = []
                    sub_len = 0
                    for word in words:
                        # Pathological single token safety
                        if len(word) > max_chars:
                            if sub_words:
                                chunk_index += 1
                                chunks.append(
                                    TTSChunk(
                                        index=chunk_index,
                                        text=" ".join(sub_words),
                                        segment_order=seg_order,
                                        is_section_end=False,
                                    )
                                )
                                sub_words = []
                                sub_len = 0
                            for w_part in [word[j:j+max_chars] for j in range(0, len(word), max_chars)]:
                                chunk_index += 1
                                chunks.append(
                                    TTSChunk(
                                        index=chunk_index,
                                        text=w_part,
                                        segment_order=seg_order,
                                        is_section_end=False,
                                    )
                                )
                            continue

                        if sub_len + len(word) + 1 <= max_chars:
                            sub_words.append(word)
                            sub_len += len(word) + 1
                        else:
                            if sub_words:
                                chunk_index += 1
                                chunks.append(
                                    TTSChunk(
                                        index=chunk_index,
                                        text=" ".join(sub_words),
                                        segment_order=seg_order,
                                        is_section_end=False,
                                    )
                                )
                            sub_words = [word]
                            sub_len = len(word)
                    if sub_words:
                        chunk_index += 1
                        chunks.append(
                            TTSChunk(
                                index=chunk_index,
                                text=" ".join(sub_words),
                                segment_order=seg_order,
                                is_section_end=False,
                            )
                        )
                else:
                    current_chunk_sentences.append(sentence)
                    current_len = sentence_len

        if current_chunk_sentences:
            chunk_index += 1
            chunks.append(
                TTSChunk(
                    index=chunk_index,
                    text=" ".join(current_chunk_sentences),
                    segment_order=seg_order,
                    is_section_end=not is_last_segment,
                )
            )

    return chunks
