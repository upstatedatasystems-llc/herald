"""
Centralized Secret Redaction and Diagnostics Sanitization Service for Herald.
Provides robust allowlist and pattern-based secret scrubbing across loggers,
exception handlers, Telegram messages, and exported support bundles.
"""

import os
import platform
import re
import sys
from typing import Any

from herald.config import settings

# Generic secret patterns
GENERIC_PATTERNS = [
    (re.compile(r"/bot\d+:[a-zA-Z0-9_-]+/", re.IGNORECASE), "/bot[REDACTED_BOT_TOKEN]/"),
    (
        re.compile(r"(x-goog-api-key['\"]?\s*[:=]\s*['\"]?)([^'\"\s\r\n&]+)(['\"]?)", re.IGNORECASE),
        r"\1[REDACTED_API_KEY]\3",
    ),
    (
        re.compile(r"(x-api-key['\"]?\s*[:=]\s*['\"]?)([^'\"\s\r\n&]+)(['\"]?)", re.IGNORECASE),
        r"\1[REDACTED_API_KEY]\3",
    ),
    (
        re.compile(r"(authorization['\"]?\s*[:=]\s*['\"]?(?:Bearer\s+)?)([^'\"\s\r\n]+)(['\"]?)", re.IGNORECASE),
        r"\1[REDACTED_AUTH]\3",
    ),
    (
        re.compile(r"(postgres(?:ql)?://[^:]+:)([^@]+)(@)", re.IGNORECASE),
        r"\1[REDACTED_PASSWORD]\3",
    ),
    (
        re.compile(r"(password['\"]?\s*[:=]\s*['\"]?)([^'\"\s\r\n,]+)(['\"]?)", re.IGNORECASE),
        r"\1[REDACTED_PASSWORD]\3",
    ),
]

# Sensitive dictionary keys for metadata redaction
SENSITIVE_METADATA_KEYS = {
    "api_key",
    "token",
    "secret",
    "password",
    "credential",
    "auth",
    "authorization",
    "bot_token",
    "gemini_api_key",
    "anthropic_api_key",
    "openai_api_key",
    "groq_api_key",
    "openrouter_api_key",
    "mistral_api_key",
    "cloudflare_api_token",
    "cloudflare_account_id",
    "deepseek_api_key",
    "telegram_bot_token",
    "herald_api_key",
    "postgres_password",
    "delivery_nudge_secret",
    "source_text",
    "raw_text",
    "prompt",
    "contents",
    "email_body",
}


def get_known_secret_map() -> dict[str, str]:
    """Retrieve map of secret_label -> secret_value for all configured secrets."""
    secret_map: dict[str, str] = {}

    for attr in (
        "TELEGRAM_BOT_TOKEN",
        "GEMINI_API_KEY",
        "HERALD_API_KEY",
        "POSTGRES_PASSWORD",
        "DELIVERY_NUDGE_SECRET",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "MISTRAL_API_KEY",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
    ):
        val = getattr(settings, attr, None)
        if val and isinstance(val, str) and len(val.strip()) >= 4:
            secret_map[attr] = val.strip()

    # Environment extras
    for env_k, env_v in os.environ.items():
        if any(s in env_k.lower() for s in ("token", "key", "secret", "password", "auth")):
            if env_v and len(env_v.strip()) >= 4:
                secret_map[env_k] = env_v.strip()

    # Database URL password
    db_url = settings.get_database_url()
    if "@" in db_url and "://" in db_url:
        try:
            userinfo = db_url.split("://", 1)[1].split("@", 1)[0]
            if ":" in userinfo:
                pw = userinfo.split(":", 1)[1]
                if pw and len(pw) >= 4:
                    secret_map["DATABASE_PASSWORD"] = pw
        except Exception:
            pass

    return secret_map


def _get_known_secrets() -> list[str]:
    """Retrieve all configured secret values sorted descending by length."""
    secret_map = get_known_secret_map()
    return sorted(list(set(secret_map.values())), key=len, reverse=True)


def redact_text(text: str | None) -> str:
    """Scrub all known secrets and generic authorization patterns from string."""
    if not text:
        return ""

    result = str(text)
    for secret in _get_known_secrets():
        if secret in result:
            result = result.replace(secret, "[REDACTED]")

    for pattern, placeholder in GENERIC_PATTERNS:
        result = pattern.sub(placeholder, result)

    return result


# Safe numeric telemetry keys allowed through dictionary scrubbing when numeric or None
SAFE_NUMERIC_TELEMETRY_KEYS = {
    "prompt_tokens",
    "completion_tokens",
    "candidate_tokens",
    "total_tokens",
    "thought_tokens",
    "requested_max_output_tokens",
}


def redact_dict(d: dict[str, Any] | None) -> dict[str, Any]:
    """
    Recursively scrub a metadata dictionary for sensitive keys and secret values.
    Used for arbitrary runtime/debug metadata.
    Preserves exact numeric telemetry fields (e.g. thought_tokens, prompt_tokens) when numeric or None.
    """
    if not d or not isinstance(d, dict):
        return {}

    cleaned: dict[str, Any] = {}
    for k, v in d.items():
        k_str = str(k)
        k_lower = k_str.lower()

        # Narrow safe exception for exact numeric telemetry fields (integers, floats, None)
        if k_lower in SAFE_NUMERIC_TELEMETRY_KEYS:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cleaned[k_str] = v
                continue
            elif v is None:
                cleaned[k_str] = None
                continue
            else:
                # Disallow non-numeric values for token telemetry keys to prevent secret smuggling
                cleaned[k_str] = "[REDACTED]"
                continue

        if any(bad in k_lower for bad in SENSITIVE_METADATA_KEYS):
            cleaned[k_str] = "[REDACTED]"
        elif isinstance(v, dict):
            cleaned[k_str] = redact_dict(v)
        elif isinstance(v, list):
            cleaned[k_str] = [
                redact_dict(item) if isinstance(item, dict)
                else (redact_text(item) if isinstance(item, str) else item)
                for item in v
            ]
        elif isinstance(v, str):
            cleaned[k_str] = redact_text(v)
        elif isinstance(v, (int, float, bool, type(None))):
            cleaned[k_str] = v
        else:
            cleaned[k_str] = redact_text(str(v))
    return cleaned


def sanitize_content_dict(d: dict[str, Any] | None) -> dict[str, Any]:
    """
    Sanitize content artifacts (such as script JSON or research dossier),
    preserving narrative fields (narration, headings, titles) while scrubbing actual configured secrets.
    """
    if not d or not isinstance(d, dict):
        return {}

    cleaned: dict[str, Any] = {}
    # Only scrub truly credential keys, not semantic content keys like 'narration' or 'prompt'
    credential_keys = {"api_key", "token", "secret", "password", "credential", "auth", "authorization", "bot_token"}
    for k, v in d.items():
        k_str = str(k)
        k_lower = k_str.lower()

        # Narrow safe exception for exact numeric telemetry fields
        if k_lower in SAFE_NUMERIC_TELEMETRY_KEYS:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cleaned[k_str] = v
                continue
            elif v is None:
                cleaned[k_str] = None
                continue
            else:
                cleaned[k_str] = "[REDACTED]"
                continue

        if any(bad in k_lower for bad in credential_keys):
            cleaned[k_str] = "[REDACTED]"
        elif isinstance(v, dict):
            cleaned[k_str] = sanitize_content_dict(v)
        elif isinstance(v, list):
            cleaned[k_str] = [
                sanitize_content_dict(item) if isinstance(item, dict)
                else (redact_text(item) if isinstance(item, str) else item)
                for item in v
            ]
        elif isinstance(v, str):
            cleaned[k_str] = redact_text(v)
        elif isinstance(v, (int, float, bool, type(None))):
            cleaned[k_str] = v
        else:
            cleaned[k_str] = redact_text(str(v))
    return cleaned


def scan_for_secrets(content: bytes | str | None) -> list[str]:
    """
    Scan text or raw byte content against all configured secrets.
    Returns a list of secret labels that were detected (e.g. ['GEMINI_API_KEY']), or [] if clean.
    Never returns the secret values themselves.
    """
    if not content:
        return []

    if isinstance(content, bytes):
        content_str = content.decode("utf-8", errors="ignore")
    else:
        content_str = str(content)

    detected: list[str] = []
    secret_map = get_known_secret_map()

    for label, secret in secret_map.items():
        if secret and secret in content_str:
            detected.append(label)

    return list(set(detected))


def sanitize_error(error: Exception | str | None) -> tuple[str, str]:
    """
    Sanitize an error or exception into a standard (error_category, error_message) pair.
    Guarantees that no raw API keys, tokens, or credentials leak in exception text.
    """
    if error is None:
        return "NONE", ""

    if isinstance(error, Exception):
        cat = error.__class__.__name__
        msg = str(error)
    else:
        msg = str(error)
        cat = "UNKNOWN_ERROR"

    sanitized_msg = redact_text(msg)

    # Classify error category
    lower_msg = sanitized_msg.lower()
    if "outputtruncated" in cat.lower() or "truncated" in lower_msg or "max_tokens" in lower_msg or "token limit" in lower_msg:
        cat = "OUTPUT_TRUNCATED"
    elif "auth" in lower_msg or "401" in lower_msg or "403" in lower_msg or "permission" in lower_msg or "api key" in lower_msg:
        cat = "AUTHENTICATION_FAILED"
    elif "429" in lower_msg or "quota" in lower_msg or "rate limit" in lower_msg:
        cat = "RATE_LIMIT_EXCEEDED"
    elif "timeout" in lower_msg or "timed out" in lower_msg:
        cat = "TIMEOUT"
    elif "validation" in lower_msg or "schema" in lower_msg or "json" in lower_msg:
        cat = "SCHEMA_VALIDATION_ERROR"
    elif "network" in lower_msg or "connection" in lower_msg or "connect" in lower_msg:
        cat = "NETWORK_ERROR"
    elif "candidate" in lower_msg or "empty" in lower_msg or "no response" in lower_msg:
        cat = "EMPTY_RESPONSE"

    return cat, sanitized_msg


def build_safe_environment_summary() -> dict[str, Any]:
    """
    Construct an allowlist-based environment and system summary for support diagnostics.
    Does NOT dump raw environment variables or credentials.
    """
    from herald.concurrency import resolve_concurrency_settings

    conc = resolve_concurrency_settings(
        profile=settings.HERALD_CONCURRENCY_PROFILE,
        worker_concurrency=settings.HERALD_WORKER_CONCURRENCY,
        script_concurrency=settings.HERALD_SCRIPT_CONCURRENCY,
        tts_global_slots=settings.HERALD_TTS_GLOBAL_SLOTS,
        tts_per_job=settings.HERALD_TTS_PER_JOB,
        ffmpeg_concurrency=settings.HERALD_FFMPEG_CONCURRENCY,
        n8n_concurrency=settings.HERALD_N8N_CONCURRENCY,
    )

    return {
        "system": {
            "platform": sys.platform,
            "os_name": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
        },
        "herald": {
            "environment": settings.HERALD_ENV,
            "default_mode": settings.get_default_mode(),
            "ai_configured": settings.is_ai_configured(),
            "ai_provider": settings.AI_PROVIDER,
            "research_provider": getattr(settings, "RESEARCH_PROVIDER", "gemini"),
            "concurrency_profile": conc.profile,
            "worker_concurrency": conc.worker_concurrency,
            "script_concurrency": conc.script_concurrency,
            "tts_global_slots": conc.tts_global_slots,
            "tts_per_job": conc.tts_per_job,
            "ffmpeg_concurrency": conc.ffmpeg_concurrency,
            "default_voice": settings.KOKORO_VOICE,
            "default_speed": settings.KOKORO_SPEED,
            "allowed_voices": settings.get_allowed_voices_list(),
            "telegram_transport_enabled": settings.ENABLE_TELEGRAM_TRANSPORT,
            "email_transport_enabled": settings.ENABLE_EMAIL_TRANSPORT,
            "metrics_enabled": settings.HERALD_METRICS_ENABLED,
        },
    }
