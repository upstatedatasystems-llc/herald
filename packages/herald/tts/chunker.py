import re


class TTSChunk:
    def __init__(self, index: int, text: str, segment_sequence: int):
        self.index = index
        self.text = text
        self.segment_sequence = segment_sequence


def split_text_into_sentences(text: str) -> list[str]:
    """
    Split text into sentences preserving sentence-ending punctuation.
    Avoids splitting on common abbreviations like Mr., Mrs., Dr., vs., etc.
    """
    if not text:
        return []

    # Pattern protecting common abbreviations
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
    preserving sentence and paragraph boundaries.
    """
    chunks: list[TTSChunk] = []
    chunk_index = 0

    for seg in script_segments:
        seg_seq = seg.get("sequence", 1)
        text = seg.get("text", "").strip()

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
                            segment_sequence=seg_seq,
                        )
                    )
                    current_chunk_sentences = []
                    current_len = 0

                # Handle exceptionally long single sentences
                if sentence_len > max_chars:
                    # Break long sentence on space boundaries
                    words = sentence.split(" ")
                    sub_words: list[str] = []
                    sub_len = 0
                    for word in words:
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
                                        segment_sequence=seg_seq,
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
                                segment_sequence=seg_seq,
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
                    segment_sequence=seg_seq,
                )
            )

    return chunks
