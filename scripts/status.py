#!/usr/bin/env python3
"""
Herald Status CLI Script.
Queries database and reports system health and active job queue counts.
"""

import sys

from sqlalchemy import text

from herald.config import settings
from herald.db.connection import SessionLocal
from herald.db.models import JobState, PodcastJob


def get_status_report():
    print("=" * 60)
    print(" HERALD EMAIL-TO-PODCAST SYSTEM STATUS ")
    print("=" * 60)
    print(f"Environment: {settings.HERALD_ENV}")
    print(f"Database Host: {settings.POSTGRES_HOST}")
    print(f"Work Directory: {settings.HERALD_WORK_DIR}")

    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        print("Database Connection: OK")
    except Exception as e:
        print(f"Database Connection: ERROR ({e})")
        sys.exit(1)

    print("-" * 60)
    print("JOB QUEUE SUMMARY BY STATE:")
    print("-" * 60)

    for state in JobState:
        count = db.query(PodcastJob).filter(PodcastJob.status == state.value).count()
        if count > 0:
            print(f"  {state.value:<20}: {count}")

    total_jobs = db.query(PodcastJob).count()
    print("-" * 60)
    print(f"TOTAL JOBS IN SYSTEM: {total_jobs}")
    print("=" * 60)

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(get_status_report())
