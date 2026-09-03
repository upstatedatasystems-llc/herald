"""
CLI helper for Herald setup pairing code inspection and generation.
Used non-interactively by setup.sh and automated installation scripts.
"""

import sys

from herald.db.connection import SessionLocal
from herald.telegram.auth import get_or_create_active_pairing_code, has_owner


def get_pairing_status(expires_in_minutes: int = 30) -> str:
    """
    Check if an owner is paired or retrieve/create an active pairing code.
    Returns:
        'PAIRED' if owner already exists.
        'UNPAIRED:<code>:<expires_in_minutes>' if unpaired.
        'ERROR:<detail>' on failure.
    """
    with SessionLocal() as db:
        if has_owner(db):
            return "PAIRED"
        code = get_or_create_active_pairing_code(db, expires_in_minutes=expires_in_minutes)
        if code:
            return f"UNPAIRED:{code}:{expires_in_minutes}"
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
