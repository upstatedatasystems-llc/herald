import hashlib
import re
from enum import Enum

from bs4 import BeautifulSoup

from herald.config import settings
from herald.db.models import RequestMode


class SourceClassification(str, Enum):
    EMAIL_BODY = "email_body"
    URL = "url"
    INVALID_MULTIPLE_URLS = "invalid_multiple_urls"
    EMPTY = "empty"


class EmailParseResult:
    def __init__(
        self,
        mode: RequestMode,
        clean_text: str,
        detected_url: str | None,
        source_hash: str,
        classification: SourceClassification,
        custom_voice: str | None = None,
        custom_speed: float | None = None,
        custom_title: str | None = None,
    ):
        self.mode = mode
        self.clean_text = clean_text
        self.detected_url = detected_url
        self.source_hash = source_hash
        self.classification = classification
        self.custom_voice = custom_voice
        self.custom_speed = custom_speed
        self.custom_title = custom_title


EXACT_SUBJECT_MAP = {
    "podcast: brief": RequestMode.BRIEF,
    "podcast: standard": RequestMode.STANDARD,
    "podcast: detailed": RequestMode.DETAILED,
}

DIRECTIVE_PATTERNS = {
    "voice": re.compile(r"^\s*Voice\s*:\s*([a-zA-Z0-9_-]+)\s*$", re.IGNORECASE),
    "speed": re.compile(r"^\s*Speed\s*:\s*([0-9.]+)\s*$", re.IGNORECASE),
    "title": re.compile(r"^\s*Title\s*:\s*(.+)\s*$", re.IGNORECASE),
}

GENERIC_DIRECTIVE_PATTERN = re.compile(r"^\s*([a-zA-Z0-9_-]+)\s*:\s*(.*)$")

REPLY_SEPARATOR_PATTERNS = [
    re.compile(r"^\s*--\s*$", re.MULTILINE),
    re.compile(r"^\s*On\s+.*wrote:\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*From:\s+.*Sent:\s+.*To:\s+.*Subject:\s+.*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*_{10,}\s*$", re.MULTILINE),
    re.compile(r"^\s*Unsubscribe\b.*$", re.MULTILINE | re.IGNORECASE),
]

URL_REGEX = re.compile(
    r"https?://(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:/[^\s<>'\"\)]*)?"
)

PERMITTED_URL_LABELS = {
    "article:", "link:", "source:", "url:", "article", "link", "source", "url"
}


def parse_subject_mode(subject: str) -> RequestMode | None:
    """
    Parse email subject. Repeatedly strip leading Re:, Fwd:, FW: prefixes conservatively.
    Require the entire remaining normalized subject to match EXACTLY one valid command.
    """
    if not subject:
        return None

    clean = subject.strip()
    old_clean = None

    while clean != old_clean:
        old_clean = clean
        clean = re.sub(r"^\s*(?:Re|Fwd|FW|RE|FWD)\s*:\s*", "", clean, flags=re.IGNORECASE).strip()

    normalized = clean.lower()
    return EXACT_SUBJECT_MAP.get(normalized)


def parse_directives(text: str) -> tuple[str, str | None, float | None, str | None]:
    """
    Parse optional top-of-body directives (Voice:, Speed:, Title:) from first non-empty lines.
    Rejects duplicate directives, unknown directives, or overlong titles with ValueError.
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
    seen_directives = set()

    for line in lines:
        stripped = line.strip()
        if not stripped and in_header_zone:
            continue

        if in_header_zone:
            m_voice = DIRECTIVE_PATTERNS["voice"].match(stripped)
            if m_voice:
                if "voice" in seen_directives:
                    raise ValueError("Duplicate directive 'Voice:' detected.")
                seen_directives.add("voice")
                val = m_voice.group(1).strip().lower()
                if val not in allowed_voices:
                    raise ValueError(f"Invalid directive 'Voice: {val}'. Voice must be one of: {allowed_voices}")
                custom_voice = val
                continue

            m_speed = DIRECTIVE_PATTERNS["speed"].match(stripped)
            if m_speed:
                if "speed" in seen_directives:
                    raise ValueError("Duplicate directive 'Speed:' detected.")
                seen_directives.add("speed")
                try:
                    s_val = float(m_speed.group(1))
                    if not (settings.MIN_SPEED <= s_val <= settings.MAX_SPEED):
                        raise ValueError(f"Invalid directive 'Speed: {s_val}'. Speed must be between {settings.MIN_SPEED} and {settings.MAX_SPEED}.")
                    custom_speed = s_val
                except ValueError as ve:
                    raise ValueError(f"Invalid directive 'Speed: {m_speed.group(1)}': {ve}")
                continue

            m_title = DIRECTIVE_PATTERNS["title"].match(stripped)
            if m_title:
                if "title" in seen_directives:
                    raise ValueError("Duplicate directive 'Title:' detected.")
                seen_directives.add("title")
                t_val = m_title.group(1).strip()
                if len(t_val) == 0:
                    raise ValueError("Directive 'Title:' cannot be empty.")
                if len(t_val) > 255:
                    raise ValueError(f"Directive 'Title:' exceeds maximum length of 255 characters (got {len(t_val)}).")
                custom_title = t_val
                continue

            m_generic = GENERIC_DIRECTIVE_PATTERN.match(stripped)
            if m_generic:
                key = m_generic.group(1).strip()
                if key.lower() not in ("voice", "speed", "title", "http", "https", "article", "link", "source", "url"):
                    raise ValueError(f"Unknown or invalid directive '{key}:'. Allowed directives are Voice:, Speed:, Title:")

        in_header_zone = False
        remaining_lines.append(line)

    clean_text_without_directives = "\n".join(remaining_lines).strip()
    return clean_text_without_directives, custom_voice, custom_speed, custom_title


def clean_email_text(text: str) -> str:
    """
    Remove quoted reply history, signatures, tracking parameters, and noise.
    """
    if not text:
        return ""

    lines = text.splitlines()
    cleaned_lines = []

    for line in lines:
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
    """Convert HTML email content into clean readable plain text."""
    if not html_content:
        return ""

    soup = BeautifulSoup(html_content, "html.parser")
    for element in soup(["script", "style", "nav", "footer", "header", "form", "aside"]):
        element.extract()

    text = soup.get_text(separator="\n")
    return clean_email_text(text)


def extract_urls(text: str) -> list[str]:
    """Extract all HTTP/HTTPS URLs from text, stripping trailing punctuation."""
    urls = URL_REGEX.findall(text)
    clean_urls = []
    for url in urls:
        cleaned = url.rstrip(".,;!?)>]\":'")
        if cleaned and cleaned not in clean_urls:
            clean_urls.append(cleaned)
    return clean_urls


def classify_source_content(clean_text: str, urls: list[str]) -> tuple[SourceClassification, str | None]:
    """
    Classify source text into email_body, url, invalid_multiple_urls, or empty.
    """
    if not clean_text:
        return SourceClassification.EMPTY, None

    if not urls:
        return SourceClassification.EMAIL_BODY, None

    if len(urls) > 1:
        non_url_text = URL_REGEX.sub("", clean_text).strip()
        words = non_url_text.split()
        if len(words) < 15:
            return SourceClassification.INVALID_MULTIPLE_URLS, None
        return SourceClassification.EMAIL_BODY, None

    target_url = urls[0]
    non_url_text = URL_REGEX.sub("", clean_text).strip()
    cleaned_label = non_url_text.lower().strip()

    if not cleaned_label or cleaned_label in PERMITTED_URL_LABELS:
        return SourceClassification.URL, target_url

    return SourceClassification.EMAIL_BODY, None


def compute_source_hash(text: str, canonical_url: str | None = None) -> str:
    """Compute a deterministic SHA-256 fingerprint for deduplication."""
    content = text.strip()
    if canonical_url:
        content += f"||url={canonical_url.strip()}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def process_email_message(
    subject: str, body_text: str | None = None, body_html: str | None = None
) -> EmailParseResult:
    """
    Process incoming email content, check subject mode, parse directives, clean body, classify source type.
    """
    mode = parse_subject_mode(subject)
    if not mode:
        raise ValueError(
            f"Unsupported subject line: '{subject}'. Expected exact subject: 'Podcast: Brief', 'Podcast: Standard', or 'Podcast: Detailed'."
        )

    if body_text and len(body_text.strip()) > 20:
        raw_content = body_text
    elif body_html:
        raw_content = html_to_text(body_html)
    else:
        raw_content = body_text or ""

    clean_content, custom_voice, custom_speed, custom_title = parse_directives(raw_content)
    clean_content = clean_email_text(clean_content)

    if not clean_content or len(clean_content) > settings.HERALD_MAX_SOURCE_CHARS:
        raise ValueError(
            f"Content length ({len(clean_content)} chars) is invalid or exceeds limit of {settings.HERALD_MAX_SOURCE_CHARS} characters."
        )

    urls = extract_urls(clean_content)
    classification, detected_url = classify_source_content(clean_content, urls)

    if classification == SourceClassification.INVALID_MULTIPLE_URLS:
        raise ValueError("Multiple URLs submitted without substantive context. Please send one URL per request.")

    source_hash = compute_source_hash(clean_content, detected_url if classification == SourceClassification.URL else None)

    return EmailParseResult(
        mode=mode,
        clean_text=clean_content,
        detected_url=detected_url,
        source_hash=source_hash,
        classification=classification,
        custom_voice=custom_voice,
        custom_speed=custom_speed,
        custom_title=custom_title,
    )
