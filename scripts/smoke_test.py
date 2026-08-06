#!/usr/bin/env python3
import sys
import os
import argparse
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from packages.herald.config import settings
from packages.herald.tts.kokoro_client import KokoroClient, KokoroTTSError
from packages.herald.audio.ffmpeg_builder import join_and_normalize_audio


def run_smoke_test(mock_if_missing: bool = False):
    if mock_if_missing:
        os.environ["HERALD_MOCK_TTS"] = "1"

    print("Running Herald Kokoro TTS & FFmpeg Audio Smoke Test...")

    client = KokoroClient()
    h_status = client.health_check()

    print(f"Health Check Status: {h_status}")

    if not h_status["ffmpeg"] and not mock_if_missing:
        print("ERROR: FFmpeg is not installed or not in PATH.")
        sys.exit(1)

    if not h_status["kokoro_api"] and not mock_if_missing:
        print("ERROR: Kokoro API service is not reachable at", settings.KOKORO_BASE_URL)
        sys.exit(1)

    test_dir = Path(settings.HERALD_WORK_DIR) / "smoke_test"
    test_dir.mkdir(parents=True, exist_ok=True)

    chunk_file = test_dir / "smoke_chunk.wav"
    output_mp3 = test_dir / "smoke_output.mp3"

    sample_text = "Welcome to Herald. This is a synthetic audio test for email to podcast automation."

    try:
        print("Synthesizing test audio chunk...")
        client.synthesize_chunk(text=sample_text, output_path=chunk_file)
        print(f"Chunk created successfully: {chunk_file} ({chunk_file.stat().st_size} bytes)")

        print("Assembling and normalizing MP3 output with FFmpeg...")
        res = join_and_normalize_audio(
            chunk_paths=[chunk_file],
            output_mp3_path=output_mp3,
            episode_title="Herald Smoke Test Episode",
            episode_description="Verification test for Kokoro and FFmpeg pipeline",
            job_id="smoke-test-job-001",
        )

        print("--------------------------------------------------")
        print("SMOKE TEST SUCCESSFUL!")
        print(f"Output MP3:        {res['output_path']}")
        print(f"Duration:          {res['duration_seconds']} seconds")
        print(f"File Size:         {res['file_bytes']} bytes")
        print(f"SHA256 Checksum:   {res['sha256']}")
        print("--------------------------------------------------")

        # Cleanup
        try:
            chunk_file.unlink(missing_ok=True)
            output_mp3.unlink(missing_ok=True)
        except Exception:
            pass

    except Exception as e:
        print(f"SMOKE TEST FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Herald TTS & Audio Smoke Test")
    parser.add_argument(
        "--mock-if-missing",
        action="store_true",
        help="Use mock TTS if Kokoro server/model is not running",
    )
    args = parser.parse_args()
    run_smoke_test(mock_if_missing=args.mock_if_missing)
