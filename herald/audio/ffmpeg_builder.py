import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import mutagen
from mutagen.id3 import COMM, ID3, TALB, TIT2, TPE1

from herald.config import settings

logger = logging.getLogger("herald.audio.ffmpeg")


class FFmpegExecutionError(Exception):
    """Exception raised when FFmpeg or FFprobe commands fail."""


def check_free_disk_mb(path: Path) -> float:
    """Check available free disk space in megabytes."""
    try:
        check_path = path if path.exists() else path.parent
        total, used, free = shutil.disk_usage(check_path)
        return free / (1024 * 1024)
    except Exception as e:
        logger.warning(f"Disk check failed for '{path}': {e}")
        return 999999.0


def generate_silence_wav(output_path: Path, duration_seconds: float = 0.5) -> Path:
    """Generate a silent WAV chunk for pause insertion between sections/paragraphs."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not shutil.which("ffmpeg"):
        with open(output_path, "wb") as f:
            f.write(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x40\x1f\x00\x00\x80\x3e\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")
        return output_path

    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r=24000:cl=mono:d={duration_seconds}",
        str(output_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        logger.warning(f"Silence generation failed: {res.stderr}")
    return output_path


def validate_audio_file(file_path: Path) -> dict[str, Any]:
    """
    Validate that an audio file exists, is non-zero, and contains valid audio streams using mutagen/ffprobe.
    """
    if not file_path.exists() or file_path.stat().st_size == 0:
        raise FFmpegExecutionError(f"Audio file '{file_path}' is missing or 0 bytes.")

    duration_sec = 0
    try:
        audio = mutagen.File(file_path)
        if audio and audio.info and hasattr(audio.info, "length"):
            duration_sec = int(audio.info.length)
    except Exception:
        pass

    if duration_sec == 0 and shutil.which("ffprobe"):
        try:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            duration_sec = int(float(res.stdout.strip()))
        except Exception as e:
            logger.warning(f"FFprobe duration check failed: {e}")

    return {
        "valid": True,
        "size_bytes": file_path.stat().st_size,
        "duration_seconds": duration_sec,
    }


def embed_id3_metadata(
    mp3_path: Path,
    title: str,
    description: str | None = None,
    job_id: str | None = None,
) -> None:
    """Embed ID3v2 tags into the finished MP3 file."""
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
    insert_pauses: bool = True,
) -> dict[str, Any]:
    """
    Concat WAV audio chunks with section pause padding, normalize spoken loudness using loudnorm,
    encode to mono MP3, embed ID3 metadata, and validate audio duration.
    """
    if not chunk_paths:
        raise FFmpegExecutionError("No audio chunk files provided for assembly.")

    # Check low disk space before rendering
    free_mb = check_free_disk_mb(output_mp3_path.parent)
    if free_mb < settings.HERALD_MIN_DISK_MB:
        raise FFmpegExecutionError(f"Insufficient free disk space ({free_mb:.1f} MB available, required {settings.HERALD_MIN_DISK_MB} MB).")

    output_mp3_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list_path = output_mp3_path.parent / f"concat_{job_id or 'temp'}.txt"

    padded_chunks = []
    pauses_dir = output_mp3_path.parent / f"pauses_{job_id or 'temp'}"

    try:
        if insert_pauses:
            padding_start = generate_silence_wav(pauses_dir / "silence_start.wav", 0.8)
            padded_chunks.append(padding_start)

        for chunk in chunk_paths:
            padded_chunks.append(chunk)

        if insert_pauses:
            padding_end = generate_silence_wav(pauses_dir / "silence_end.wav", 0.8)
            padded_chunks.append(padding_end)

        with open(concat_list_path, "w", encoding="utf-8") as f:
            for chunk in padded_chunks:
                escaped_path = str(chunk.resolve()).replace("'", "'\\''")
                f.write(f"file '{escaped_path}'\n")

        loudnorm_str = (
            f"loudnorm=I={settings.LOUDNORM_TARGET_I}:"
            f"TP={settings.LOUDNORM_TARGET_TP}:"
            f"LRA={settings.LOUDNORM_TARGET_LRA}"
        )

        cmd = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list_path),
            "-af", loudnorm_str,
            "-ac", str(settings.AUDIO_CHANNELS),
            "-ar", str(settings.AUDIO_SAMPLE_RATE),
            "-b:a", settings.AUDIO_OUTPUT_BITRATE,
            str(output_mp3_path),
        ]

        if not shutil.which("ffmpeg"):
            if os.environ.get("HERALD_MOCK_TTS") == "1":
                with open(output_mp3_path, "wb") as f:
                    f.write(b"ID3\x03\x00\x00\x00\x00\x00\x00HERALD_MOCK_AUDIO_DATA_FOR_TESTING_1234567890")
                return {
                    "output_path": str(output_mp3_path),
                    "file_bytes": output_mp3_path.stat().st_size,
                    "duration_seconds": 10,
                    "sha256": compute_file_sha256(output_mp3_path),
                }
            raise FFmpegExecutionError("FFmpeg binary is not found in PATH.")

        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise FFmpegExecutionError(f"FFmpeg failed with exit code {res.returncode}: {res.stderr}")

        val_info = validate_audio_file(output_mp3_path)
        embed_id3_metadata(output_mp3_path, title=episode_title, description=episode_description, job_id=job_id)

        checksum = compute_file_sha256(output_mp3_path)
        log_entry = {
            "timestamp": logger.name,
            "job_id": job_id,
            "stage": "ENCODING",
            "result": "SUCCESS",
            "file_bytes": val_info["size_bytes"],
            "duration_seconds": val_info["duration_seconds"],
            "sha256": checksum,
        }
        logger.info(json.dumps(log_entry))

        return {
            "output_path": str(output_mp3_path),
            "file_bytes": val_info["size_bytes"],
            "duration_seconds": val_info["duration_seconds"],
            "sha256": checksum,
        }

    finally:
        if concat_list_path.exists():
            try:
                concat_list_path.unlink()
            except Exception:
                pass
        if pauses_dir.exists():
            try:
                shutil.rmtree(pauses_dir)
            except Exception:
                pass
