import hashlib
import re
from enum import Enum

from bs4 import BeautifulSoup

from herald.config import settings
from herald.db.models import RequestMode
from herald.extraction.source_cleaner import clean_source_text


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
        research_depth: str | None = None,
        tts_chunk_chars: int = 500,
        verify_final_script: bool = False,
        warnings: list[str] | None = None,
    ):
        self.mode = mode
        self.clean_text = clean_text
        self.detected_url = detected_url
        self.source_hash = source_hash
        self.classification = classification
        self.custom_voice = custom_voice
        self.custom_speed = custom_speed
        self.custom_title = custom_title
        self.research_depth = research_depth
        self.tts_chunk_chars = tts_chunk_chars
        self.verify_final_script = verify_final_script
        self.warnings = warnings or []


BASE_SUBJECT_PATTERNS = [
    ("podcast: literal", RequestMode.LITERAL, None),
    ("podcast: research low", RequestMode.RESEARCH, "low"),
    ("podcast: research medium", RequestMode.RESEARCH, "medium"),
    ("podcast: research high", RequestMode.RESEARCH, "high"),
    ("podcast: research", RequestMode.RESEARCH, None),
    ("podcast: detailed", RequestMode.RESEARCH, "medium"),
    ("podcast: brief", RequestMode.BRIEF, None),
    ("podcast: standard", RequestMode.STANDARD, None),
]

EXACT_SUBJECT_MAP = {
    "podcast: literal": (RequestMode.LITERAL, None),
    "podcast: brief": (RequestMode.BRIEF, None),
    "podcast: standard": (RequestMode.STANDARD, None),
    "podcast: detailed": (RequestMode.RESEARCH, "medium"),
    "podcast: research": (RequestMode.RESEARCH, None),
    "podcast: research low": (RequestMode.RESEARCH, "low"),
    "podcast: research medium": (RequestMode.RESEARCH, "medium"),
    "podcast: research high": (RequestMode.RESEARCH, "high"),
}

DIRECTIVE_PATTERNS = {
    "voice": re.compile(r"^\s*Voice\s*:\s*([a-zA-Z0-9_-]+)\s*$", re.IGNORECASE),
    "speed": re.compile(r"^\s*Speed\s*:\s*([0-9.]+)\s*$", re.IGNORECASE),
    "title": re.compile(r"^\s*Title\s*:\s*(.+)\s*$", re.IGNORECASE),
    "research": re.compile(r"^\s*Research\s*:\s*([a-zA-Z0-9_-]+)\s*$", re.IGNORECASE),
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


def parse_subject_directives(subject: str) -> tuple[RequestMode | None, str | None, int, bool]:
    """
    Parse email subject with optional, case-insensitive, order-independent commands.
    Supports base mode prefixes (Brief, Standard, Research [Low|Medium|High], Detailed)
    followed by optional directives: 'chunk-N' (min..max) and 'verify'.
    Returns (RequestMode, research_depth, tts_chunk_chars, verify_final_script).
    """
    default_chunk = getattr(settings, "TTS_CHUNK_DEFAULT_CHARS", 500)
    min_chunk = getattr(settings, "TTS_CHUNK_MIN_CHARS", 250)
    max_chunk = getattr(settings, "TTS_CHUNK_MAX_CHARS", 1000)

    if not subject:
        return None, None, default_chunk, False

    clean = subject.strip()
    old_clean = None

    while clean != old_clean:
        old_clean = clean
        clean = re.sub(r"^\s*(?:Re|Fwd|FW|RE|FWD)\s*:\s*", "", clean, flags=re.IGNORECASE).strip()

    normalized = clean.lower()
    matched_mode = None
    matched_depth = None
    remainder = ""

    for prefix, mode, depth in BASE_SUBJECT_PATTERNS:
        if normalized == prefix or normalized.startswith(prefix + " "):
            matched_mode = mode
            matched_depth = depth
            remainder = clean[len(prefix):].strip()
            break

    if not matched_mode:
        return None, None, default_chunk, False

    tts_chunk_chars = default_chunk
    verify_final_script = False
    seen_chunk_cmd = False

    if remainder:
        tokens = remainder.split()
        for token in tokens:
            token_norm = token.strip().lower()
            if not token_norm:
                continue

            if token_norm.startswith("chunk-"):
                if seen_chunk_cmd:
                    raise ValueError(f"Conflicting or duplicate chunk command '{token}' in subject line.")
                seen_chunk_cmd = True
                val_str = token_norm[len("chunk-"):]
                if not val_str.isdigit():
                    raise ValueError(f"Malformed chunk command '{token}' in subject line. Expected format 'chunk-N' (e.g. chunk-500).")
                c_val = int(val_str)
                if not (min_chunk <= c_val <= max_chunk):
                    raise ValueError(f"Invalid chunk size '{c_val}' in subject line. Chunk size must be between {min_chunk} and {max_chunk} characters.")
                tts_chunk_chars = c_val
            elif token_norm == "verify":
                verify_final_script = True
            else:
                raise ValueError(f"Unknown or invalid subject command '{token}' in subject line. Allowed commands are 'verify' and 'chunk-N'.")

    return matched_mode, matched_depth, tts_chunk_chars, verify_final_script


def parse_subject_mode_and_depth(subject: str) -> tuple[RequestMode, str | None] | tuple[None, None]:
    """
    Parse email subject. Repeatedly strip leading Re:, Fwd:, FW: prefixes conservatively.
    Returns (RequestMode, depth_from_subject) or (None, None).
    """
    try:
        mode, depth, _, _ = parse_subject_directives(subject)
        if mode:
            return mode, depth
    except ValueError:
        pass
    return None, None


def parse_subject_mode(subject: str) -> RequestMode | None:
    """Convenience wrapper returning RequestMode or None for backwards compatibility."""
    mode, _ = parse_subject_mode_and_depth(subject)
    return mode


def parse_directives(text: str) -> tuple[str, str | None, float | None, str | None, str | None]:
    """
    Parse optional top-of-body directives (Voice:, Speed:, Title:, Research:) from first non-empty lines.
    Rejects duplicate directives, unknown directives, or invalid values.
    Returns (clean_text, voice, speed, title, research_depth).
    """
    if not text:
        return text, None, None, None, None

    lines = text.splitlines()
    remaining_lines = []
    custom_voice = None
    custom_speed = None
    custom_title = None
    custom_research_depth = None

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

            m_research = DIRECTIVE_PATTERNS["research"].match(stripped)
            if m_research:
                if "research" in seen_directives:
                    raise ValueError("Duplicate directive 'Research:' detected.")
                seen_directives.add("research")
                r_val = m_research.group(1).strip().lower()
                if r_val not in ("low", "medium", "high"):
                    raise ValueError(f"Invalid directive 'Research: {r_val}'. Research depth must be one of: low, medium, high")
                custom_research_depth = r_val
                continue

            m_generic = GENERIC_DIRECTIVE_PATTERN.match(stripped)
            if m_generic:
                key = m_generic.group(1).strip()
                if key.lower() not in ("voice", "speed", "title", "research", "http", "https", "article", "link", "source", "url"):
                    raise ValueError(f"Unknown or invalid directive '{key}:'. Allowed directives are Voice:, Speed:, Title:, Research:")

        in_header_zone = False
        remaining_lines.append(line)

    clean_text_without_directives = "\n".join(remaining_lines).strip()
    return clean_text_without_directives, custom_voice, custom_speed, custom_title, custom_research_depth


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
    content = text.strip() if text else ""
    if canonical_url:
        canon_url_str = canonical_url.strip()
        non_url_text = URL_REGEX.sub("", content).strip().lower()
        if not non_url_text or non_url_text in PERMITTED_URL_LABELS:
            content = f"url={canon_url_str}"
        else:
            content = f"{content}||url={canon_url_str}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def process_email_message(
    subject: str, body_text: str | None = None, body_html: str | None = None
) -> EmailParseResult:
    """
    Process incoming email content, parse subject directives, clean body, classify source type.
    """
    mode, subject_depth, tts_chunk_chars, verify_final_script = parse_subject_directives(subject)
    if not mode:
        raise ValueError(
            f"Unsupported subject line: '{subject}'. Expected subject starting with 'Podcast: Brief', 'Podcast: Standard', or 'Podcast: Research'."
        )

    if body_text and len(body_text.strip()) > 20:
        raw_content = body_text
    elif body_html:
        raw_content = html_to_text(body_html)
    else:
        raw_content = body_text or ""

    clean_content, custom_voice, custom_speed, custom_title, body_depth = parse_directives(raw_content)
    clean_content = clean_email_text(clean_content)
    clean_content = clean_source_text(clean_content)

    if not clean_content or len(clean_content) > settings.HERALD_MAX_SOURCE_CHARS:
        raise ValueError(
            f"Content length ({len(clean_content)} chars) is invalid or exceeds limit of {settings.HERALD_MAX_SOURCE_CHARS} characters."
        )

    warnings: list[str] = []
    research_depth: str | None = None

    if mode == RequestMode.RESEARCH:
        if subject_depth:
            research_depth = subject_depth
            if body_depth and body_depth != subject_depth:
                warnings.append(f"Explicit subject research depth '{subject_depth}' overrode body directive 'Research: {body_depth}'.")
        elif body_depth:
            research_depth = body_depth
        else:
            research_depth = "medium"
    else:
        if body_depth:
            warnings.append(f"Body directive 'Research: {body_depth}' ignored for non-Research mode ({mode.value}).")

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
        research_depth=research_depth,
        tts_chunk_chars=tts_chunk_chars,
        verify_final_script=verify_final_script,
        warnings=warnings,
    )

