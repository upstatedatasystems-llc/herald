import re
from datetime import UTC, datetime


def sanitize_filename_title(title: str) -> str:
    """
    Sanitize episode title for readable, safe filenames across file systems.
    Replaces invalid characters (: / \\ ? * < > | ") with '-' while preserving spaces and readability.
    """
    if not title:
        return "Herald Episode"
    cleaned = re.sub(r'[\:\/\\?\*\<\>\|"]+', "-", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:120] or "Herald Episode"


def build_user_facing_drive_filename(
    title: str,
    created_at: datetime | None,
    mode: str,
    extension: str,
) -> str:
    """
    Build user-facing Drive filename format: <Sanitized Title> <m-d-yy> <Mode>.<ext>
    Example: The Future of AI 8-11-26 Standard.mp3
    """
    sanitized_title = sanitize_filename_title(title)
    dt = created_at or datetime.now(UTC)
    date_str = f"{dt.month}-{dt.day}-{dt.strftime('%y')}"
    safe_mode = (mode or "Standard").strip().title()
    ext = extension.lstrip(".")
    return f"{sanitized_title} {date_str} {safe_mode}.{ext}"
