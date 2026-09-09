"""
CLI helper for Herald setup pairing code inspection and generation.
Used non-interactively by setup.sh and automated installation scripts.
"""

import sys
from datetime import UTC, datetime

from herald.db.connection import SessionLocal
from herald.telegram.auth import get_or_create_active_pairing_record, has_owner


def get_pairing_status(expires_in_minutes: int = 30, read_only: bool = False) -> str:
    """
    Check if an owner is paired or retrieve/create an active pairing code with truthful remaining minutes.
    If read_only is True, returns 'NONE' if no active unexpired pairing code exists (does not generate).
    Returns:
        'PAIRED' if owner already exists.
        'UNPAIRED:<code>:<remaining_minutes>' if unpaired.
        'NONE' if read_only and no code exists.
        'ERROR:<detail>' on failure.
    """
    with SessionLocal() as db:
        if has_owner(db):
            return "PAIRED"
        if read_only:
            from herald.db.models import TelegramPairingCode

            now = datetime.now(UTC)
            record = (
                db.query(TelegramPairingCode)
                .filter(
                    TelegramPairingCode.is_used.is_(False),
                    TelegramPairingCode.expires_at > now,
                )
                .order_by(TelegramPairingCode.created_at.desc())
                .first()
            )
            if record:
                expires_at = record.expires_at if record.expires_at.tzinfo else record.expires_at.replace(tzinfo=UTC)
                remaining_secs = (expires_at - now).total_seconds()
                remaining_mins = max(1, int(round(remaining_secs / 60.0)))
                return f"UNPAIRED:{record.code}:{remaining_mins}"
            return "NONE"

        record = get_or_create_active_pairing_record(db, expires_in_minutes=expires_in_minutes)
        if record:
            now = datetime.now(UTC)
            expires_at = record.expires_at if record.expires_at.tzinfo else record.expires_at.replace(tzinfo=UTC)
            remaining_secs = (expires_at - now).total_seconds()
            remaining_mins = max(1, int(round(remaining_secs / 60.0)))
            return f"UNPAIRED:{record.code}:{remaining_mins}"
        return "ERROR:Could not generate pairing code"


def main() -> None:
    read_only = "--read-only" in sys.argv
    try:
        status = get_pairing_status(read_only=read_only)
        print(status)
        sys.exit(0)
    except Exception as e:
        print(f"ERROR:{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
