import re


PAGE_JUNK_PATTERNS = [
    re.compile(r"^\s*(?:Printable Version|Print this article|Download PDF|Share this story)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r".*?(?:Cookie Policy|Privacy Policy|Terms of Service|All rights reserved\.?).*", re.IGNORECASE),
    re.compile(r"^\s*(?:Skip to content|Skip to main content|Toggle navigation|Menu)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r".*?(?:Menu|Subscribe|Sign In)\s*\|\s*(?:Menu|Subscribe|Sign In).*", re.IGNORECASE),
]

IMAGE_ALT_PATTERN = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
LATEX_WRAPPER_PATTERN = re.compile(r"\[latex\](.*?)\[/latex\]", re.IGNORECASE | re.DOTALL)
TRACKING_URL_PARAM_PATTERN = re.compile(r"([?&])(?:utm_[a-z]+|ref|source|tracking_id|fbclid|gclid)=[^&\s]+", re.IGNORECASE)


def clean_source_text(text: str) -> str:
    """
    Shared deterministic source cleanup for both email body and URL/article sources.
    Strips obvious page furniture, navigation debris, tracking query params, and normalizes markup
    without modifying underlying semantic content, facts, numbers, or tables.
    """
    if not text:
        return ""

    cleaned = text

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
    # Collapse 3+ consecutive newlines to 2
    result = re.sub(r"\n{3,}", "\n\n", result).strip()

    return result
