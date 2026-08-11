import re
import unicodedata

PAGE_JUNK_PATTERNS = [
    re.compile(r"^\s*(?:Printable Version|Print this article|Download PDF|Share this story)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r".*?(?:Cookie Policy|Privacy Policy|Terms of Service|All rights reserved\.?).*", re.IGNORECASE),
    re.compile(r"^\s*(?:Skip to content|Skip to main content|Toggle navigation|Menu)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r".*?(?:Menu|Subscribe|Sign In)\s*\|\s*(?:Menu|Subscribe|Sign In).*", re.IGNORECASE),
]

IMAGE_ALT_PATTERN = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
LATEX_WRAPPER_PATTERN = re.compile(r"\[latex\](.*?)\[/latex\]", re.IGNORECASE | re.DOTALL)
TRACKING_URL_PARAM_PATTERN = re.compile(r"([?&])(?:utm_[a-z]+|ref|source|tracking_id|fbclid|gclid)=[^&\s]+", re.IGNORECASE)


def sanitize_unicode(text: str) -> str:
    """
    Safely normalize Unicode using NFC and strip illegal control characters
    without destroying legitimate diacritical, accented, or non-ASCII characters.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFC", text)
    clean_chars = []
    for ch in normalized:
        cat = unicodedata.category(ch)
        if cat.startswith("C") and ch not in ("\n", "\r", "\t"):
            continue
        clean_chars.append(ch)
    return "".join(clean_chars)


def deduplicate_source_blocks(text: str) -> tuple[str, dict]:
    """
    Detect and deduplicate contiguous repeated paragraph blocks (e.g., identical 1,500+ word article pasted twice back-to-back).
    Preserves legitimate short headings and repeated phrases.
    Returns (normalized_text, stats_dict).
    """
    if not text or not text.strip():
        return text or "", {
            "original_word_count": 0,
            "normalized_word_count": 0,
            "original_char_count": 0,
            "normalized_char_count": 0,
            "blocks_removed": 0,
        }

    orig_char_count = len(text)
    orig_words = len(text.split())

    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]

    # If the block sequence has exact contiguous repetition (e.g. A B C D A B C D)
    n = len(blocks)
    if n >= 2 and n % 2 == 0:
        half = n // 2
        first_half = blocks[:half]
        second_half = blocks[half:]
        if first_half == second_half and sum(len(b) for b in first_half) > 50:
            blocks = first_half

    # Deduplicate contiguous identical large blocks (> 100 chars)
    deduped_blocks = []
    blocks_removed = 0
    for b in blocks:
        if deduped_blocks and b == deduped_blocks[-1] and len(b) > 100:
            blocks_removed += 1
            continue
        deduped_blocks.append(b)

    normalized_text = "\n\n".join(deduped_blocks)
    norm_char_count = len(normalized_text)
    norm_words = len(normalized_text.split())

    stats = {
        "original_word_count": orig_words,
        "normalized_word_count": norm_words,
        "original_char_count": orig_char_count,
        "normalized_char_count": norm_char_count,
        "blocks_removed": blocks_removed,
    }
    return normalized_text, stats


def clean_source_text(text: str) -> str:
    """
    Shared deterministic source cleanup for both email body and URL/article sources.
    Strips obvious page furniture, navigation debris, tracking query params, and normalizes markup
    without modifying underlying semantic content, facts, numbers, or tables.
    """
    if not text:
        return ""

    cleaned = sanitize_unicode(text)

    # Remove [latex]...[/latex] wrappers while preserving contents
    cleaned = LATEX_WRAPPER_PATTERN.sub(r"\1", cleaned)

    # Normalize image alt markdown: ![alt text](url) -> alt text
    cleaned = IMAGE_ALT_PATTERN.sub(r"\1", cleaned)

    # Strip tracking parameters from embedded URLs in text
    cleaned = TRACKING_URL_PARAM_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"[?&]\s*$", "", cleaned)

    # Filter out obvious page junk lines
    lines = cleaned.splitlines()
    filtered_lines = []

    for line in lines:
        stripped = line.strip()
        if any(pat.match(stripped) for pat in PAGE_JUNK_PATTERNS):
            continue
        filtered_lines.append(line)

    result = "\n".join(filtered_lines)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()

    return result
