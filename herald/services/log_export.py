"""
Log and Diagnostics Export Service for Herald.

Collects and packages Herald operational logs and canonical diagnostics within
a caller-specified time window into a downloadable ZIP archive.
"""

from __future__ import annotations

import logging
import re
import zipfile
from datetime import datetime, tzinfo
from pathlib import Path

from herald.config import settings

logger = logging.getLogger("herald.services.log_export")

# Timestamp format produced by standard logging %(asctime)s:
# e.g., 2026-09-13 18:31:02,123 [LEVEL] [logger.name] message
_LOG_TIMESTAMP_REGEX = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+\["
)

# Command argument pattern: YYYY-MM-DD [HH:MM]
_LOGS_ARG_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?$"
)


class LogsArgumentError(ValueError):
    """Raised when user-provided /logs arguments are invalid or malformed."""


def get_configured_timezone(tz_name: str | None = None) -> tzinfo:
    """
    Resolve a timezone-aware tzinfo object.
    Prefers zoneinfo.ZoneInfo, with graceful fallback to dateutil.tz or UTC
    for cross-platform compatibility (e.g. Windows environments without system IANA tzdata).
    """
    name = tz_name or getattr(settings, "TZ", "America/New_York")
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        try:
            import dateutil.tz

            tz = dateutil.tz.gettz(name)
            if tz is not None:
                return tz
        except Exception:
            pass
        from datetime import timezone

        return timezone.utc


def parse_logs_command_args(
    args_str: str,
    tz: tzinfo | None = None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """
    Parse and validate the date/time arguments for the /logs command.

    Accepted syntax:
        YYYY-MM-DD
        YYYY-MM-DD HH:MM

    Returns:
        tuple of (start_datetime, end_datetime) both aware in tz.

    Raises:
        LogsArgumentError on missing, malformed, invalid, or future dates/times.
    """
    effective_tz = tz or get_configured_timezone()

    cleaned = (args_str or "").strip()
    if not cleaned:
        raise LogsArgumentError(
            "Usage:\n"
            "<code>/logs YYYY-MM-DD [HH:MM]</code>\n\n"
            "Examples:\n"
            "<code>/logs 2026-09-13</code>\n"
            "<code>/logs 2026-09-13 18:30</code>"
        )

    match = _LOGS_ARG_PATTERN.match(cleaned)
    if not match:
        raise LogsArgumentError(
            "Invalid date/time syntax. Expected <code>YYYY-MM-DD</code> or <code>YYYY-MM-DD HH:MM</code>.\n\n"
            "Examples:\n"
            "<code>/logs 2026-09-13</code>\n"
            "<code>/logs 2026-09-13 18:30</code>"
        )

    date_str, time_str = match.groups()

    # Validate calendar date
    try:
        dt_parsed = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise LogsArgumentError(
            f"Invalid calendar date: <code>{date_str}</code>."
        )

    # Validate clock time
    if time_str:
        try:
            t_parsed = datetime.strptime(time_str, "%H:%M")
        except ValueError:
            raise LogsArgumentError(
                f"Invalid clock time: <code>{time_str}</code>. Must be 24-hour HH:MM (00:00 - 23:59)."
            )
        start_dt = dt_parsed.replace(
            hour=t_parsed.hour,
            minute=t_parsed.minute,
            second=0,
            microsecond=0,
            tzinfo=effective_tz,
        )
    else:
        start_dt = dt_parsed.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
            tzinfo=effective_tz,
        )

    # Capture end timestamp
    export_end = now if now is not None else datetime.now(effective_tz)
    if export_end.tzinfo is None:
        export_end = export_end.replace(tzinfo=effective_tz)

    if start_dt > export_end:
        raise LogsArgumentError(
            "The requested start time cannot be in the future."
        )

    return start_dt, export_end


def generate_export_zip_name(start_dt: datetime, end_dt: datetime) -> str:
    """
    Generate canonical filename for the outer logs export ZIP bundle.
    Uses local timestamp formatting: herald-logs-YYYYMMDD-HHMM-to-YYYYMMDD-HHMM.zip
    """
    s_str = start_dt.strftime("%Y%m%d-%H%M")
    e_str = end_dt.strftime("%Y%m%d-%H%M")
    return f"herald-logs-{s_str}-to-{e_str}.zip"


def parse_log_timestamp(line: str, tz: tzinfo) -> datetime | None:
    """
    Extract and parse the leading timestamp from a standard Herald log line.
    Returns timezone-aware datetime in tz, or None if the line does not start with a valid timestamp.
    """
    m = _LOG_TIMESTAMP_REGEX.match(line)
    if not m:
        return None

    raw_ts = m.group(1).replace(",", ".")
    # Try parsing with microseconds/fractions, then without
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw_ts, fmt)
            return dt.replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def filter_log_file_content(
    content: str,
    start_dt: datetime,
    end_dt: datetime,
    tz: tzinfo,
) -> tuple[str, int]:
    """
    Filter the lines of a log file, keeping only records whose initial timestamp
    falls within [start_dt, end_dt].

    Multiline exception tracebacks and continuation lines stay attached to their
    parent record (included if the parent record is included, excluded if the parent
    record is excluded). Lines preceding the first recognized record are discarded.

    Returns:
        (filtered_content_str, matching_record_count)
    """
    lines = content.splitlines(keepends=True)
    kept_lines: list[str] = []
    current_record_included = False
    matching_record_count = 0

    for line in lines:
        ts = parse_log_timestamp(line, tz)
        if ts is not None:
            # Beginning of a new log record
            if start_dt <= ts <= end_dt:
                current_record_included = True
                matching_record_count += 1
                kept_lines.append(line)
            else:
                current_record_included = False
        else:
            # Continuation line or traceback line
            if current_record_included:
                kept_lines.append(line)

    return "".join(kept_lines), matching_record_count


def collect_matching_service_logs(
    log_dir: Path,
    start_dt: datetime,
    end_dt: datetime,
    tz: tzinfo,
) -> tuple[dict[str, str], int]:
    """
    Scan persistent service log files in log_dir, filter their records by timestamp,
    and return non-empty filtered log contents mapped to their archive paths.

    Returns:
        (dict_of_archive_path_to_content, total_matching_records)
    """
    if not log_dir.exists() or not log_dir.is_dir():
        return {}, 0

    matched_logs: dict[str, str] = {}
    total_records = 0

    try:
        candidates = sorted(
            [
                p
                for p in log_dir.iterdir()
                if p.is_file()
                and (".log" in p.name)
                and not p.name.endswith(".zip")
                and not p.name.endswith(".tmp")
                and ".tmp." not in p.name
            ],
            key=lambda p: p.name,
        )
    except Exception as e:
        logger.error("Error scanning log directory '%s': %s", log_dir, e)
        return {}, 0

    for file_path in candidates:
        try:
            raw_content = file_path.read_text(encoding="utf-8", errors="replace")
            filtered_content, count = filter_log_file_content(
                raw_content, start_dt, end_dt, tz
            )
            if count > 0 and filtered_content.strip():
                rel_path = f"logs/{file_path.name}"
                matched_logs[rel_path] = filtered_content
                total_records += count
        except Exception as e:
            logger.warning("Failed to process log file '%s': %s", file_path, e)

    return matched_logs, total_records


def collect_matching_diagnostics(
    diagnostics_dir: Path,
    start_dt: datetime,
    end_dt: datetime,
    tz: tzinfo,
) -> list[Path]:
    """
    Scan diagnostics_dir for canonical support ZIP archives whose filesystem modification
    timestamp falls within [start_dt, end_dt].

    Ignores directories, temporary .tmp files, and non-zip files.
    """
    if not diagnostics_dir.exists() or not diagnostics_dir.is_dir():
        return []

    matched_zips: list[Path] = []

    try:
        for entry in diagnostics_dir.iterdir():
            if not entry.is_file():
                continue
            if not entry.name.endswith(".zip"):
                continue
            if entry.name.startswith(".") or ".tmp" in entry.name:
                continue

            try:
                mtime = entry.stat().st_mtime
                mtime_dt = datetime.fromtimestamp(mtime, tz=tz)
                if start_dt <= mtime_dt <= end_dt:
                    matched_zips.append(entry)
            except Exception as e:
                logger.warning(
                    "Failed to check mtime for diagnostic archive '%s': %s",
                    entry,
                    e,
                )
    except Exception as e:
        logger.error("Error reading diagnostics directory '%s': %s", diagnostics_dir, e)

    matched_zips.sort(key=lambda p: p.name)
    return matched_zips


def build_log_export_archive(
    output_zip_path: Path,
    filtered_logs: dict[str, str],
    diagnostic_zips: list[Path],
) -> None:
    """
    Build the outer ZIP archive containing filtered service logs and nested canonical
    diagnostics ZIPs.
    """
    output_zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for archive_path, content in filtered_logs.items():
            zf.writestr(archive_path, content.encode("utf-8"))

        for diag_path in diagnostic_zips:
            arcname = f"logs/diagnostics/{diag_path.name}"
            zf.write(diag_path, arcname=arcname)
