"""
CLI helper for Herald setup pairing code inspection and generation.
Used non-interactively by setup.sh and automated installation scripts.
"""

import sys
from datetime import UTC, datetime

from herald.db.connection import SessionLocal
from herald.telegram.auth import get_or_create_active_pairing_record, has_owner


def get_pairing_status(expires_in_minutes: int = 30) -> str:
    """
    Check if an owner is paired or retrieve/create an active pairing code with truthful remaining minutes.
    Returns:
        'PAIRED' if owner already exists.
        'UNPAIRED:<code>:<remaining_minutes>' if unpaired.
        'ERROR:<detail>' on failure.
    """
    with SessionLocal() as db:
        if has_owner(db):
            return "PAIRED"
        record = get_or_create_active_pairing_record(db, expires_in_minutes=expires_in_minutes)
        if record:
            now = datetime.now(UTC)
            expires_at = (
                record.expires_at
                if record.expires_at.tzinfo
                else record.expires_at.replace(tzinfo=UTC)
            )
            remaining_secs = (expires_at - now).total_seconds()
            remaining_mins = max(1, int(round(remaining_secs / 60.0)))
            return f"UNPAIRED:{record.code}:{remaining_mins}"
        return "ERROR:Could not generate pairing code"


def main() -> None:
    try:
        status = get_pairing_status()
        print(status)
        sys.exit(0)
    except Exception as e:
        print(f"ERROR:{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
