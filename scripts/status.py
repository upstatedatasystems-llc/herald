#!/usr/bin/env python3
import sys
import os
import shutil
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from packages.herald.config import settings
from packages.herald.db.connection import SessionLocal
from packages.herald.db.models import PodcastJob, JobState
from packages.herald.tts.kokoro_client import KokoroClient


def print_status():
    print("==================================================")
    print("       HERALD AUTOMATION SYSTEM STATUS            ")
    print("==================================================")

    # 1. Environment & Config
    print(f"Environment:       {settings.HERALD_ENV}")
    print(f"Work Directory:    {settings.HERALD_WORK_DIR}")
    print(f"Allowed Senders:   {settings.EMAIL_ALLOWED_SENDERS or '(None configured)'}")
    print(f"Drive Folder ID:   {settings.GOOGLE_DRIVE_FOLDER_ID or '(None configured)'}")

    # 2. Database Connection & Queue Metrics
    print("\n--- Database Queue Status ---")
    try:
        db = SessionLocal()
        total_jobs = db.query(PodcastJob).count()
        queued = db.query(PodcastJob).filter(PodcastJob.status == JobState.QUEUED.value).count()
        synthesizing = db.query(PodcastJob).filter(PodcastJob.status == JobState.SYNTHESIZING.value).count()
        completed = db.query(PodcastJob).filter(PodcastJob.status == JobState.COMPLETE.value).count()
        failed = db.query(PodcastJob).filter(PodcastJob.status == JobState.FAILED.value).count()

        print(f"Total Jobs:        {total_jobs}")
        print(f"Queued Jobs:       {queued}")
        print(f"Active Jobs:       {synthesizing}")
        print(f"Completed Jobs:    {completed}")
        print(f"Failed Jobs:       {failed}")

        latest_complete = (
            db.query(PodcastJob)
            .filter(PodcastJob.status == JobState.COMPLETE.value)
            .order_by(PodcastJob.completed_at.desc())
            .first()
        )
        if latest_complete:
            print(f"Last Success Job:  {latest_complete.id} ({latest_complete.completed_at})")
            print(f"Drive Link:        {latest_complete.drive_web_link}")

        db.close()
    except Exception as e:
        print(f"Database Connection Error: {e}")

    # 3. Component & Dependency Health
    print("\n--- Service & Dependency Health ---")
    kokoro_client = KokoroClient()
    h_status = kokoro_client.health_check()

    print(f"FFmpeg Installed:  {'YES' if h_status['ffmpeg'] else 'NO'}")
    print(f"Kokoro API Ready:  {'YES' if h_status['kokoro_api'] else 'NO'}")
    print(f"Kokoro Models:     {'YES' if h_status['model_path_exists'] else 'NO'}")

    # 4. Storage Usage
    print("\n--- Disk Storage ---")
    work_path = Path(settings.HERALD_WORK_DIR)
    if work_path.exists():
        total, used, free = shutil.disk_usage(work_path)
        print(f"Disk Free Space:   {free / 1024 / 1024 / 1024:.2f} GB (Total: {total / 1024 / 1024 / 1024:.2f} GB)")
    else:
        print(f"Work directory '{work_path}' does not exist locally yet.")

    print("==================================================")


if __name__ == "__main__":
    print_status()
