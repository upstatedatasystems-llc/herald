import logging
import re
from typing import Any

_SECRET_PATTERNS = []


def register_secret_for_redaction(secret: str, placeholder: str = "[REDACTED]") -> None:
    """Register a secret token/key for redaction across all emitted log messages."""
    if secret and len(secret.strip()) >= 4:
        escaped = re.escape(secret.strip())
        _SECRET_PATTERNS.append((re.compile(escaped), placeholder))


class SecretRedactingFormatter(logging.Formatter):
    """Logging formatter that scrubs registered secrets and tokens before output."""

    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        for pattern, placeholder in _SECRET_PATTERNS:
            s = pattern.sub(placeholder, s)
        # Redact generic bot token patterns in URLs, e.g. /bot123456:ABC.../
        s = re.sub(r"/bot\d+:[a-zA-Z0-9_-]+/", "/bot[REDACTED_BOT_TOKEN]/", s)
        # Redact x-goog-api-key or authorization headers
        s = re.sub(r"(x-goog-api-key['\"]?:\s*['\"])[^'\"]+(['\"])", r"\1[REDACTED_API_KEY]\2", s, flags=re.IGNORECASE)
        s = re.sub(r"(authorization['\"]?:\s*['\"](?:Bearer\s+)?)[^'\"]+(['\"])", r"\1[REDACTED_AUTH]\2", s, flags=re.IGNORECASE)
        return s


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs record arguments and message before propagation."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            msg = record.msg
            for pattern, placeholder in _SECRET_PATTERNS:
                msg = pattern.sub(placeholder, msg)
            msg = re.sub(r"/bot\d+:[a-zA-Z0-9_-]+/", "/bot[REDACTED_BOT_TOKEN]/", msg)
            msg = re.sub(r"(x-goog-api-key['\"]?:\s*['\"])[^'\"]+(['\"])", r"\1[REDACTED_API_KEY]\2", msg, flags=re.IGNORECASE)
            msg = re.sub(r"(authorization['\"]?:\s*['\"](?:Bearer\s+)?)[^'\"]+(['\"])", r"\1[REDACTED_AUTH]\2", msg, flags=re.IGNORECASE)
            record.msg = msg
        return True


def setup_secure_logging() -> None:
    """Configure secret redaction across root and library loggers."""
    from herald.config import settings

    if settings.TELEGRAM_BOT_TOKEN:
        register_secret_for_redaction(settings.TELEGRAM_BOT_TOKEN, "[REDACTED_BOT_TOKEN]")
    if settings.GEMINI_API_KEY:
        register_secret_for_redaction(settings.GEMINI_API_KEY, "[REDACTED_API_KEY]")
    if settings.HERALD_API_KEY:
        register_secret_for_redaction(settings.HERALD_API_KEY, "[REDACTED_HERALD_API_KEY]")

    root = logging.getLogger()
    redacting_filter = SecretRedactingFilter()
    root.addFilter(redacting_filter)

    for h in root.handlers:
        h.addFilter(redacting_filter)
        if h.formatter:
            fmt = h.formatter._fmt if hasattr(h.formatter, "_fmt") else "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
            h.setFormatter(SecretRedactingFormatter(fmt))

    # Suppress verbose httpx/httpcore request line logging in production
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
