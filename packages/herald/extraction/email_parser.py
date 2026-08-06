import hashlib
import re

from bs4 import BeautifulSoup

from packages.herald.db.models import RequestMode


class EmailParseResult:

    def __init__(
        self,
        mode: RequestMode,
        clean_text: str,
        detected_url: str | None,
        source_hash: str,
        is_url_dominant: bool = False,
    ):
        self.mode = mode
        self.clean_text = clean_text
        self.detected_url = detected_url
        self.source_hash = source_hash
        self.is_url_dominant = is_url_dominant


# Regex patterns for subject mode extraction
SUBJECT_MODE_PATTERNS = [
    (re.compile(r"podcast\s*:\s*brief", re.IGNORECASE), RequestMode.BRIEF),
    (re.compile(r"podcast\s*:\s*standard", re.IGNORECASE), RequestMode.STANDARD),
    (re.compile(r"podcast\s*:\s*detailed", re.IGNORECASE), RequestMode.DETAILED),
]

# Common email signatures, reply separators, and newsletter footers
REPLY_SEPARATOR_PATTERNS = [
    re.compile(r"^\s*--\s*$", re.MULTILINE),  # Standard signature delimiter
    re.compile(r"^\s*On\s+.*wrote:\s*$", re.MULTILINE | re.IGNORECASE),  # Reply header
    re.compile(
        r"^\s*From:\s+.*Sent:\s+.*To:\s+.*Subject:\s+.*$", re.MULTILINE | re.IGNORECASE
    ),  # Outlook reply header
    re.compile(r"^\s*_{10,}\s*$", re.MULTILINE),  # Line dividers
    re.compile(
        r"^\s*Unsubscribe\b.*$", re.MULTILINE | re.IGNORECASE
    ),  # Unsubscribe footers
]

# URL extraction pattern
URL_REGEX = re.compile(
    r"https?://(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:/[^\s<>'\"\)]*)?"
)


def parse_subject_mode(subject: str) -> RequestMode | None:
    if not subject:
        return None
    subject_trimmed = subject.strip()
    for pattern, mode in SUBJECT_MODE_PATTERNS:
        if pattern.search(subject_trimmed):
            return mode
    return None


def clean_email_text(text: str) -> str:
    """
    Remove quoted reply history, email signatures, tracking parameters, and common noise.
    """
    if not text:
        return ""

    lines = text.splitlines()
    cleaned_lines = []

    for line in lines:
        # Stop processing if a clear reply/signature delimiter is met
        for pattern in REPLY_SEPARATOR_PATTERNS:
            if pattern.match(line):
                break
        else:
            # Check line prefix for blockquote symbols
            if line.strip().startswith(">"):
                continue
            cleaned_lines.append(line)
            continue
        break

    result = "\n".join(cleaned_lines)
    # Normalize multiple newlines
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def html_to_text(html_content: str) -> str:
    """
    Convert HTML email content into clean readable plain text.
    """
    if not html_content:
        return ""

    soup = BeautifulSoup(html_content, "html.parser")

    # Remove script, style, header, footer, nav elements
    for element in soup(["script", "style", "nav", "footer", "header", "form"]):
        element.extract()

    text = soup.get_text(separator="\n")
    return clean_email_text(text)


def extract_urls(text: str) -> list[str]:
    """
    Extract all HTTP/HTTPS URLs from text, stripping trailing punctuation.
    """
    urls = URL_REGEX.findall(text)
    clean_urls = []
    for url in urls:
        # Strip trailing punctuation
        cleaned = url.rstrip(".,;!?)>]\":'")
        if cleaned and cleaned not in clean_urls:
            clean_urls.append(cleaned)
    return clean_urls


def compute_source_hash(text: str, canonical_url: str | None = None) -> str:
    """
    Compute a deterministic SHA-256 fingerprint for deduplication.
    """
    content = text.strip()
    if canonical_url:
        content += f"||url={canonical_url.strip()}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def process_email_message(
    subject: str, body_text: str | None = None, body_html: str | None = None
) -> EmailParseResult:
    """
    Process incoming email content, determine request mode, clean body, and extract URL if present.
    """
    mode = parse_subject_mode(subject) or RequestMode.STANDARD

    # Extract body text from plain text or HTML
    if body_text and len(body_text.strip()) > 50:
        raw_content = body_text
    elif body_html:
        raw_content = html_to_text(body_html)
    else:
        raw_content = body_text or ""

    clean_content = clean_email_text(raw_content)

    # Detect URLs
    urls = extract_urls(clean_content)
    detected_url = urls[0] if urls else None

    # Check if URL is dominant (e.g., short body text consisting mostly of a single URL)
    is_url_dominant = False
    if detected_url and len(clean_content.strip()) < 300:
        is_url_dominant = True

    source_hash = compute_source_hash(clean_content, detected_url if is_url_dominant else None)

    return EmailParseResult(
        mode=mode,
        clean_text=clean_content,
        detected_url=detected_url,
        source_hash=source_hash,
        is_url_dominant=is_url_dominant,
    )
