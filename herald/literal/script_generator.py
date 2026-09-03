import re

from herald.ai.schema import PodcastScriptResponse, PodcastSegment
from herald.extraction.source_cleaner import (
    clean_source_text,
    deduplicate_source_blocks,
    sanitize_unicode,
)


def normalize_tts_spoken_text(text: str) -> str:
    """
    Deterministically clean text for Kokoro TTS without changing underlying facts or prose.
    - Strips markdown formatting (bold, italic, strikethrough, backticks)
    - Replaces raw URL links [text](http...) -> text
    - Converts raw URLs into friendly spoken references
    - Cleans bullet points and list dashes
    - Normalizes multiple punctuation and spaces
    """
    if not text:
        return ""

    s = sanitize_unicode(text)

    # Markdown links: [text](url) -> text
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)

    # Bare URLs: replace with domain or brief mention
    s = re.sub(r"https?://(?:www\.)?([a-zA-Z0-9.-]+)(?:/[^\s]*)?", r"\1", s)

    # Bold/Italic formatting
    s = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", s)

    # Code backticks
    s = re.sub(r"`([^`]+)`", r"\1", s)

    # Headings markers at start of lines: # Title -> Title
    s = re.sub(r"^\s*#{1,6}\s*", "", s, flags=re.MULTILINE)

    # Bullet markers at start of lines
    s = re.sub(r"^\s*[-*•]\s+", "", s, flags=re.MULTILINE)

    # Numbered list markers: 1. Item -> Item
    s = re.sub(r"^\s*\d+\.\s+", "", s, flags=re.MULTILINE)

    # Collapse excessive whitespace within lines
    s = re.sub(r"[ \t]+", " ", s)

    return s.strip()


def extract_title_and_body(source_text: str, custom_title: str | None = None) -> tuple[str, str]:
    """
    Extract title and remaining body from source text.
    Prefers custom_title if provided.
    Otherwise checks for 'Title: ...' or leading markdown heading.
    """
    if custom_title and custom_title.strip():
        title = custom_title.strip()
        body = source_text
        if body.lower().startswith("title:"):
            lines = body.splitlines()
            body = "\n".join(lines[1:]).strip()
        return title, body

    lines = [line.strip() for line in source_text.splitlines() if line.strip()]
    if not lines:
        return "Herald Episode", source_text

    first_line = lines[0]
    if first_line.lower().startswith("title:"):
        title = first_line[6:].strip()
        body = "\n".join(lines[1:]).strip()
        return title or "Herald Episode", body

    if first_line.startswith("#"):
        title = re.sub(r"^#+\s*", "", first_line).strip()
        body = "\n".join(lines[1:]).strip()
        return title or "Herald Episode", body

    return "Herald Episode", source_text


def generate_literal_script(
    source_text: str,
    source_title: str | None = None,
    max_segment_chars: int = 1200,
) -> PodcastScriptResponse:
    """
    Generate a PodcastScriptResponse completely deterministically on local host
    without making any LLM or external network requests.
    """
    cleaned = clean_source_text(source_text)
    deduped, _ = deduplicate_source_blocks(cleaned)

    title, body = extract_title_and_body(deduped, custom_title=source_title)
    if not body.strip():
        body = deduped.strip() or "No source text provided."

    # Split body into logical blocks/paragraphs
    raw_blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
    if not raw_blocks:
        raw_blocks = [body.strip()]

    # Assemble segments respecting heading boundaries and character limits
    segments: list[PodcastSegment] = []
    current_heading = "Introduction"
    current_narration_chunks: list[str] = []
    current_length = 0
    segment_idx = 1

    def flush_segment():
        nonlocal current_narration_chunks, current_length, segment_idx, current_heading
        if current_narration_chunks:
            narration_text = " ".join(current_narration_chunks).strip()
            cleaned_narration = normalize_tts_spoken_text(narration_text)
            if cleaned_narration:
                segments.append(
                    PodcastSegment(
                        order=segment_idx,
                        heading=current_heading or f"Section {segment_idx}",
                        narration=cleaned_narration,
                    )
                )
                segment_idx += 1
            current_narration_chunks = []
            current_length = 0

    for block in raw_blocks:
        # Check if block is a markdown or explicit heading
        if (block.startswith("#") or block.lower().startswith("section") or block.lower().startswith("chapter")) and len(block) < 100:
            flush_segment()
            heading_clean = re.sub(r"^#+\s*", "", block).strip()
            current_heading = heading_clean or f"Section {segment_idx}"
            continue

        spoken_block = normalize_tts_spoken_text(block)
        if not spoken_block:
            continue

        if current_length + len(spoken_block) > max_segment_chars and current_narration_chunks:
            flush_segment()
            if current_heading == "Introduction" and segment_idx > 1:
                current_heading = f"Reading Part {segment_idx}"

        current_narration_chunks.append(spoken_block)
        current_length += len(spoken_block)

    flush_segment()

    if not segments:
        segments.append(
            PodcastSegment(
                order=1,
                heading="Narration",
                narration=normalize_tts_spoken_text(body) or "Source text reading.",
            )
        )

    total_words = sum(len(s.narration.split()) for s in segments)
    estimated_minutes = max(1, round(total_words / 140.0))

    return PodcastScriptResponse(
        episode_title=title or "Herald Episode",
        episode_description=f"Direct literal narration of source content ({total_words} words).",
        estimated_minutes=estimated_minutes,
        source_title=source_title or title,
        segments=segments,
        warnings=["Generated via Literal deterministic mode (zero LLM calls)."],
    )
