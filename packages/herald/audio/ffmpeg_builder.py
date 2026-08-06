import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import mutagen
from mutagen.id3 import COMM, ID3, TALB, TIT2, TPE1

from packages.herald.config import settings

logger = logging.getLogger("herald.audio.ffmpeg")


class FFmpegExecutionError(Exception):
    """Exception raised when FFmpeg or FFprobe commands fail."""


def get_audio_duration_seconds(file_path: Path) -> int:
    """
    Get duration of audio file in integer seconds using mutagen or ffprobe.
    """
    try:
        audio = mutagen.File(file_path)
        if audio and audio.info and hasattr(audio.info, "length"):
            return int(audio.info.length)
    except Exception as e:
        logger.warning(f"Mutagen duration check failed: {e}. Falling back to ffprobe...")

    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return int(float(res.stdout.strip()))
    except Exception as e:
        logger.error(f"FFprobe duration check failed: {e}")
        return 0


def embed_id3_metadata(
    mp3_path: Path,
    title: str,
    description: str | None = None,
    job_id: str | None = None,
) -> None:
    """
    Embed ID3v2 tags into the finished MP3 file.
    """
    try:
        try:
            tags = ID3(mp3_path)
        except Exception:
            tags = ID3()

        tags["TIT2"] = TIT2(encoding=3, text=title)
        tags["TPE1"] = TPE1(encoding=3, text="Herald Podcast Generator")
        tags["TALB"] = TALB(encoding=3, text="Herald Audio Episodes")
        if description or job_id:
            comment_text = f"{description or ''}\nJob ID: {job_id or ''}".strip()
            tags["COMM"] = COMM(encoding=3, lang="eng", desc="Episode Info", text=comment_text)

        tags.save(mp3_path)
    except Exception as e:
        logger.warning(f"Failed to embed ID3 tags on '{mp3_path}': {e}")


def compute_file_sha256(file_path: Path) -> str:
    """
    Calculate SHA-256 checksum of an audio file.
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def join_and_normalize_audio(
    chunk_paths: list[Path],
    output_mp3_path: Path,
    episode_title: str = "Herald Episode",
    episode_description: str = "",
    job_id: str = "",
) -> dict[str, Any]:
    """
    Concat WAV audio chunks, normalize spoken-word loudness using loudnorm filter,
    encode to mono MP3, embed ID3 metadata, and calculate checksum + duration.
    """
    if not chunk_paths:
        raise FFmpegExecutionError("No audio chunk files provided for assembly.")

    output_mp3_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list_path = output_mp3_path.parent / f"concat_{job_id or 'temp'}.txt"

    # Write concat manifest file
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for chunk in chunk_paths:
            escaped_path = str(chunk.resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped_path}'\n")

    try:
        # Loudnorm filter parameters
        loudnorm_str = (
            f"loudnorm=I={settings.LOUDNORM_TARGET_I}:"
            f"TP={settings.LOUDNORM_TARGET_TP}:"
            f"LRA={settings.LOUDNORM_TARGET_LRA}"
        )

        cmd = [
            "ffmpeg",
            "-y",  # Overwrite output file
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list_path),
            "-af", loudnorm_str,
            "-ac", str(settings.AUDIO_CHANNELS),  # Mono audio
            "-ar", str(settings.AUDIO_SAMPLE_RATE),  # 24kHz sample rate
            "-b:a", settings.AUDIO_OUTPUT_BITRATE,  # 64k bitrate
            str(output_mp3_path),
        ]

        logger.info(f"Executing FFmpeg audio assembly command for job '{job_id}'")

        # Check if ffmpeg binary exists; if missing and mock mode enabled, create synthetic MP3
        if not shutil.which("ffmpeg"):
            if os.environ.get("HERALD_MOCK_TTS") == "1":
                logger.warning("FFmpeg binary not found in PATH. Generating mock MP3 file for testing...")
                with open(output_mp3_path, "wb") as f:
                    f.write(b"ID3\x03\x00\x00\x00\x00\x00\x00HERALD_MOCK_AUDIO_DATA_FOR_TESTING_1234567890")
                return {
                    "output_path": str(output_mp3_path),
                    "file_bytes": output_mp3_path.stat().st_size,
                    "duration_seconds": 10,
                    "sha256": compute_file_sha256(output_mp3_path),
                }
            raise FFmpegExecutionError("FFmpeg binary is not found in PATH.")

        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)

        if res.returncode != 0:
            raise FFmpegExecutionError(f"FFmpeg failed with exit code {res.returncode}: {res.stderr}")

        if not output_mp3_path.exists() or output_mp3_path.stat().st_size == 0:
            raise FFmpegExecutionError("FFmpeg output MP3 file is missing or 0 bytes")

        # Embed ID3 tags
        embed_id3_metadata(output_mp3_path, title=episode_title, description=episode_description, job_id=job_id)

        duration_sec = get_audio_duration_seconds(output_mp3_path)
        file_bytes = output_mp3_path.stat().st_size
        checksum = compute_file_sha256(output_mp3_path)

        return {
            "output_path": str(output_mp3_path),
            "file_bytes": file_bytes,
            "duration_seconds": duration_sec,
            "sha256": checksum,
        }

    finally:
        # Clean up concat manifest file
        if concat_list_path.exists():
            try:
                concat_list_path.unlink()
            except Exception:
                pass
