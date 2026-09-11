import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_PATTERNS = []


def clear_registered_secrets() -> None:
    """Clear all dynamically registered secret patterns."""
    _SECRET_PATTERNS.clear()


def register_secret_for_redaction(secret: str, placeholder: str = "[REDACTED]") -> None:
    """Register a secret token/key for redaction across all emitted log messages."""
    if secret and len(secret.strip()) >= 4:
        escaped = re.escape(secret.strip())
        # Avoid duplicate patterns
        for existing_pat, _ in _SECRET_PATTERNS:
            if existing_pat.pattern == escaped:
                return
        _SECRET_PATTERNS.append((re.compile(escaped), placeholder))


register_secret = register_secret_for_redaction


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
        # Redact password URLs, e.g. postgresql://user:pass@host/db
        s = re.sub(r"://([^:]+):([^@]+)@", r"://\1:[REDACTED_PASSWORD]@", s)
        return s


RedactingFormatter = SecretRedactingFormatter


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
            msg = re.sub(r"://([^:]+):([^@]+)@", r"://\1:[REDACTED_PASSWORD]@", msg)
            record.msg = msg
        return True


def register_all_configured_secrets() -> None:
    """Register all available settings credentials for redaction."""
    try:
        from herald.config import settings

        secrets_to_register = [
            (getattr(settings, "TELEGRAM_BOT_TOKEN", None), "[REDACTED_BOT_TOKEN]"),
            (getattr(settings, "GEMINI_API_KEY", None), "[REDACTED_API_KEY]"),
            (getattr(settings, "HERALD_API_KEY", None), "[REDACTED_HERALD_API_KEY]"),
            (getattr(settings, "POSTGRES_PASSWORD", None), "[REDACTED_POSTGRES_PASSWORD]"),
            (getattr(settings, "GROQ_API_KEY", None), "[REDACTED_API_KEY]"),
            (getattr(settings, "OPENROUTER_API_KEY", None), "[REDACTED_API_KEY]"),
            (getattr(settings, "MISTRAL_API_KEY", None), "[REDACTED_API_KEY]"),
            (getattr(settings, "CLOUDFLARE_API_TOKEN", None), "[REDACTED_API_KEY]"),
            (getattr(settings, "CLOUDFLARE_ACCOUNT_ID", None), "[REDACTED_ACCOUNT_ID]"),
            (getattr(settings, "ANTHROPIC_API_KEY", None), "[REDACTED_API_KEY]"),
            (getattr(settings, "OPENAI_API_KEY", None), "[REDACTED_API_KEY]"),
            (getattr(settings, "N8N_ENCRYPTION_KEY", None), "[REDACTED_ENCRYPTION_KEY]"),
            (getattr(settings, "DELIVERY_NUDGE_SECRET", None), "[REDACTED_SECRET]"),
        ]
        for secret_val, placeholder in secrets_to_register:
            if secret_val:
                register_secret_for_redaction(str(secret_val), placeholder)
    except Exception:
        pass


def setup_secure_logging() -> None:
    """Configure secret redaction across root and library loggers."""
    register_all_configured_secrets()

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


def setup_service_logging(
    service_name: str,
    log_dir: Path | str | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 1,
) -> None:
    """
    Configure idempotent dual stdout + rotating file logging for primary Herald services.
    Ensures:
      - Log messages appear exactly once in stdout and once in service log file.
      - Secrets are scrubbed across all handlers.
      - Log files rotate at max_bytes with backup_count.
      - Unwritable log directories do not crash the service (warns and continues via stdout).
    """
    from herald.config import settings

    register_all_configured_secrets()

    root = logging.getLogger()
    log_level = getattr(logging, getattr(settings, "LOG_LEVEL", "INFO").upper(), logging.INFO)
    root.setLevel(log_level)

    redacting_filter = SecretRedactingFilter()
    default_format = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    formatter = SecretRedactingFormatter(default_format)

    # Clean up non-Herald handlers to prevent double lines from previous basicConfig calls
    handlers_to_keep = []
    has_stdout = False
    file_handler_tag = f"herald_file_{service_name}"
    has_service_file = False

    for h in list(root.handlers):
        h_id = getattr(h, "_herald_handler_id", None)
        if h_id == "herald_stdout":
            has_stdout = True
            handlers_to_keep.append(h)
        elif h_id == file_handler_tag:
            has_service_file = True
            handlers_to_keep.append(h)
        elif h_id is not None and h_id.startswith("herald_file_"):
            # Handler for another service in tests
            handlers_to_keep.append(h)
        else:
            # Remove untagged handler (e.g. from basicConfig)
            root.removeHandler(h)

    # 1. Ensure exactly one stdout handler
    if not has_stdout:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(log_level)
        stdout_handler.setFormatter(formatter)
        stdout_handler.addFilter(redacting_filter)
        setattr(stdout_handler, "_herald_handler_id", "herald_stdout")
        root.addHandler(stdout_handler)

    # 2. Ensure exactly one rotating file handler for this service
    if not has_service_file:
        resolved_log_dir = Path(log_dir) if log_dir else Path(getattr(settings, "HERALD_LOG_DIR", "logs"))
        log_file_path = resolved_log_dir / f"{service_name}.log"

        try:
            resolved_log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                filename=str(log_file_path),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redacting_filter)
            setattr(file_handler, "_herald_handler_id", file_handler_tag)
            root.addHandler(file_handler)
        except Exception as e:
            sys.stderr.write(
                f"WARNING: Could not initialize persistent file logging for '{service_name}' at {log_file_path}: {e}\n"
            )

    # Suppress third-party verbose logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

