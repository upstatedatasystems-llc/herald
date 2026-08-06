import hashlib
import re

from bs4 import BeautifulSoup

from packages.herald.config import settings
from packages.herald.db.models import RequestMode


class EmailParseResult:

    def __init__(
        self,
        mode: RequestMode,
        clean_text: str,
        detected_url: str | None,
        source_hash: str,
        is_url_dominant: bool = False,
        custom_voice: str | None = None,
        custom_speed: float | None = None,
        custom_title: str | None = None,
    ):
        self.mode = mode
        self.clean_text = clean_text
        self.detected_url = detected_url
        self.source_hash = source_hash
        self.is_url_dominant = is_url_dominant
        self.custom_voice = custom_voice
        self.custom_speed = custom_speed
        self.custom_title = custom_title


# Regex patterns for subject mode extraction
SUBJECT_MODE_PATTERNS = [
    (re.compile(r"podcast\s*:\s*brief", re.IGNORECASE), RequestMode.BRIEF),
    (re.compile(r"podcast\s*:\s*standard", re.IGNORECASE), RequestMode.STANDARD),
    (re.compile(r"podcast\s*:\s*detailed", re.IGNORECASE), RequestMode.DETAILED),
]

# Patterns for optional top-of-body directives
DIRECTIVE_PATTERNS = {
    "voice": re.compile(r"^\s*Voice\s*:\s*([a-zA-Z0-9_-]+)\s*$", re.IGNORECASE),
    "speed": re.compile(r"^\s*Speed\s*:\s*([0-9.]+)\s*$", re.IGNORECASE),
    "title": re.compile(r"^\s*Title\s*:\s*(.+)\s*$", re.IGNORECASE),
}

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
    
    # Strip common reply/forward prefixes conservatively
    clean_sub = re.sub(r"^\s*(?:Re|Fwd|FW|RE):\s*", "", subject, flags=re.IGNORECASE).strip()
    
    for pattern, mode in SUBJECT_MODE_PATTERNS:
        if pattern.search(clean_sub):
            return mode
    return None


def parse_directives(text: str) -> tuple[str, str | None, float | None, str | None]:
    """
    Parse optional top-of-body directives (Voice:, Speed:, Title:) from first non-empty lines.
    Removes directives from body text and validates values against allowed bounds.
    """
    if not text:
        return text, None, None, None

    lines = text.splitlines()
    remaining_lines = []
    custom_voice = None
    custom_speed = None
    custom_title = None

    allowed_voices = settings.get_allowed_voices_list()
    in_header_zone = True

    for line in lines:
        stripped = line.strip()
        if not stripped and in_header_zone:
            continue

        matched_directive = False
        if in_header_zone:
            # Check Voice directive
            m_voice = DIRECTIVE_PATTERNS["voice"].match(stripped)
            if m_voice:
                val = m_voice.group(1).strip().lower()
                if val in allowed_voices:
                    custom_voice = val
                matched_directive = True

            # Check Speed directive
            m_speed = DIRECTIVE_PATTERNS["speed"].match(stripped)
            if m_speed:
                try:
                    s_val = float(m_speed.group(1))
                    if settings.MIN_SPEED <= s_val <= settings.MAX_SPEED:
                        custom_speed = s_val
                except ValueError:
                    pass
                matched_directive = True

            # Check Title directive
            m_title = DIRECTIVE_PATTERNS["title"].match(stripped)
            if m_title:
                custom_title = m_title.group(1).strip()[:255]
                matched_directive = True

        if matched_directive:
            continue
        else:
            in_header_zone = False
            remaining_lines.append(line)

    clean_text_without_directives = "\n".join(remaining_lines).strip()
    return clean_text_without_directives, custom_voice, custom_speed, custom_title


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
            if line.strip().startswith(">"):
                continue
            cleaned_lines.append(line)
            continue
        break

    result = "\n".join(cleaned_lines)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def html_to_text(html_content: str) -> str:
    """
    Convert HTML email content into clean readable plain text.
    """
    if not html_content:
        return ""

    soup = BeautifulSoup(html_content, "html.parser")

    for element in soup(["script", "style", "nav", "footer", "header", "form", "aside"]):
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
    Process incoming email content, determine request mode, parse directives, clean body, and evaluate URL dominance.
    """
    mode = parse_subject_mode(subject)
    if not mode:
        raise ValueError(f"Unsupported subject line: '{subject}'. Expected subject starting with 'Podcast: Brief', 'Podcast: Standard', or 'Podcast: Detailed'.")

    # Extract body text from plain text or HTML
    if body_text and len(body_text.strip()) > 20:
        raw_content = body_text
    elif body_html:
        raw_content = html_to_text(body_html)
    else:
        raw_content = body_text or ""

    # Parse directives
    clean_content, custom_voice, custom_speed, custom_title = parse_directives(raw_content)
    clean_content = clean_email_text(clean_content)

    if not clean_content or len(clean_content) > settings.HERALD_MAX_SOURCE_CHARS:
        raise ValueError(f"Content length ({len(clean_content)} chars) is invalid or exceeds limit of {settings.HERALD_MAX_SOURCE_CHARS} characters.")

    # Detect URLs
    urls = extract_urls(clean_content)
    detected_url = urls[0] if len(urls) == 1 else None

    # Dominant URL rule: exactly 1 URL present AND body text short (< 300 chars)
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
        custom_voice=custom_voice,
        custom_speed=custom_speed,
        custom_title=custom_title,
    )
