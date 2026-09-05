import hashlib
import json
import logging
import math
import os
import shutil
import struct
import subprocess
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mutagen
from mutagen.id3 import COMM, ID3, TALB, TDRC, TIT2, TPE1

from herald.config import settings

logger = logging.getLogger("herald.audio.ffmpeg")


class FFmpegExecutionError(Exception):
    """Exception raised when FFmpeg or FFprobe commands fail."""


def check_free_disk_mb(path: Path) -> float:
    """Check available free disk space in megabytes. Fail closed by raising exception."""
    try:
        check_path = path if path.exists() else path.parent
        total, used, free = shutil.disk_usage(check_path)
        _ = total
        _ = used
        return free / (1024 * 1024)
    except Exception as e:
        logger.error(f"Disk check failed for '{path}': {e}")
        raise FFmpegExecutionError(f"Disk check failed: {e}")


def generate_silence_wav(output_path: Path, duration_seconds: float = 0.5, sample_rate: int = 24000) -> Path:
    """Generate a silent WAV chunk for pause insertion between sections/paragraphs."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not shutil.which("ffmpeg"):
        num_samples = int(sample_rate * duration_seconds)
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            data = struct.pack("<" + ("h" * num_samples), *([0] * num_samples))
            wav_file.writeframes(data)
        return output_path

    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl=mono:d={duration_seconds}",
        "-ac", "1",
        "-ar", str(sample_rate),
        str(output_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    if res.returncode != 0:
        logger.warning(f"FFmpeg silence generation failed: {res.stderr}")
    return output_path


def validate_audio_file(file_path: Path) -> dict[str, Any]:
    """
    Validate that an audio file exists, is non-zero, contains valid audio streams,
    non-zero duration, and valid container format.
    For PCM WAV files, authoritatively calculates duration from WAV structure (frame_count / sample_rate)
    before generic metadata libraries to prevent streaming chunk header artifacts.
    Raises FFmpegExecutionError if file is invalid, empty, or missing streams.
    """
    if not file_path.exists() or file_path.stat().st_size == 0:
        raise FFmpegExecutionError(f"Audio file '{file_path}' is missing or 0 bytes.")

    file_size = file_path.stat().st_size
    duration_sec = 0.0
    audio_type = None
    is_valid_container = False

    # 1. Prefer authoritative wave parsing for WAV files
    is_wav = str(file_path).lower().endswith(".wav")
    if not is_wav and file_size >= 12:
        try:
            with open(file_path, "rb") as f_head:
                magic = f_head.read(12)
                if len(magic) >= 12 and magic[:4] == b"RIFF" and magic[8:12] == b"WAVE":
                    is_wav = True
        except Exception:
            pass

    if is_wav:
        try:
            with wave.open(str(file_path), "rb") as w:
                nframes = w.getnframes()
                framerate = w.getframerate()
                nchannels = w.getnchannels()
                sampwidth = w.getsampwidth()
                if framerate > 0 and nframes >= 0 and nchannels > 0 and sampwidth > 0:
                    bytes_per_frame = nchannels * sampwidth
                    max_possible_frames = max(0, file_size - 44) // bytes_per_frame
                    # Guard against unfinalized/streaming chunk headers (e.g. 0x7FFFFFFF data chunk size)
                    actual_frames = nframes
                    if actual_frames > max_possible_frames:
                        actual_frames = max_possible_frames
                    dur = actual_frames / float(framerate)
                    if math.isfinite(dur) and (dur > 0 or file_size <= 44):
                        duration_sec = float(dur)
                        is_valid_container = True
                        audio_type = "WAVE"
        except Exception as e:
            logger.debug(f"Wave inspection error for '{file_path}': {e}")

    # 2. For non-WAV formats (or if wave failed), use Mutagen metadata
    if not is_valid_container:
        try:
            audio = mutagen.File(file_path)
            if audio and audio.info:
                audio_type = type(audio).__name__
                if hasattr(audio.info, "length") and audio.info.length is not None:
                    dur = float(audio.info.length)
                    if math.isfinite(dur) and 0 < dur < 86400:
                        duration_sec = dur
                        is_valid_container = True
        except Exception as e:
            logger.debug(f"Mutagen validation error for '{file_path}': {e}")

    # 3. If still unresolved / non-positive duration, fall back to ffprobe
    if (not is_valid_container or duration_sec <= 0) and shutil.which("ffprobe"):
        try:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration,format_name:stream=codec_type",
                "-of", "json",
                str(file_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
            probe_data = json.loads(res.stdout)
            streams = probe_data.get("streams", [])
            has_audio_stream = any(s.get("codec_type") == "audio" for s in streams)
            if not has_audio_stream:
                raise FFmpegExecutionError(f"File '{file_path}' contains no valid audio streams.")

            dur_str = probe_data.get("format", {}).get("duration", "0")
            dur = float(dur_str)
            if math.isfinite(dur) and dur > 0:
                duration_sec = dur
                is_valid_container = True
                audio_type = audio_type or probe_data.get("format", {}).get("format_name", "audio")
        except Exception as e:
            if isinstance(e, FFmpegExecutionError):
                raise
            logger.debug(f"FFprobe validation fallback failed for '{file_path}': {e}")

    if not is_valid_container or duration_sec <= 0 or not math.isfinite(duration_sec):
        raise FFmpegExecutionError(f"Audio file '{file_path}' is invalid or contains no audio duration.")

    return {
        "valid": True,
        "size_bytes": file_size,
        "duration_seconds": float(duration_sec),
        "audio_type": audio_type,
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

        current_year = str(datetime.now(UTC).year)
        tags["TIT2"] = TIT2(encoding=3, text=title)
        tags["TPE1"] = TPE1(encoding=3, text="Herald Podcast Generator")
        tags["TALB"] = TALB(encoding=3, text="Herald Audio Episodes")
        tags["TDRC"] = TDRC(encoding=3, text=current_year)
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
    is_section_end_list: list[bool] | None = None,
) -> dict[str, Any]:
    """
    Concat WAV audio chunks with section (1.2s) and paragraph (0.5s) pause padding, normalize spoken loudness,
    encode to mono MP3, embed ID3 metadata, and validate audio duration.
    """
    if not chunk_paths:
        raise FFmpegExecutionError("No audio chunk files provided for assembly.")

    # Check low disk space before rendering (fail closed)
    free_mb = check_free_disk_mb(output_mp3_path.parent)
    if free_mb < settings.HERALD_MIN_DISK_MB:
        raise FFmpegExecutionError(f"Insufficient free disk space ({free_mb:.1f} MB available, required {settings.HERALD_MIN_DISK_MB} MB).")

    # Validate all input chunks before assembly
    for cp in chunk_paths:
        validate_audio_file(cp)

    output_mp3_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list_path = output_mp3_path.parent / f"concat_{job_id or 'temp'}.txt"
    pauses_dir = output_mp3_path.parent / f"pauses_{job_id or 'temp'}"

    padded_chunks = []

    try:
        if insert_pauses:
            padding_start = generate_silence_wav(pauses_dir / "silence_start.wav", 0.8)
            padded_chunks.append(padding_start)

        for i, chunk in enumerate(chunk_paths):
            padded_chunks.append(chunk)
            if insert_pauses and i < len(chunk_paths) - 1:
                is_sec_end = is_section_end_list[i] if (is_section_end_list and i < len(is_section_end_list)) else False
                pause_duration = 1.2 if is_sec_end else 0.5
                pause_wav = generate_silence_wav(pauses_dir / f"pause_{i:04d}.wav", pause_duration)
                padded_chunks.append(pause_wav)

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
            if os.environ.get("HERALD_MOCK_TTS") == "1" or getattr(settings, "HERALD_ENV", "").lower() == "test":
                with open(output_mp3_path, "wb") as f:
                    f.write(b"ID3\x03\x00\x00\x00\x00\x00\x00HERALD_MOCK_AUDIO_DATA_FOR_TESTING_1234567890")
                return {
                    "output_path": str(output_mp3_path),
                    "file_bytes": output_mp3_path.stat().st_size,
                    "duration_seconds": 10,
                    "sha256": compute_file_sha256(output_mp3_path),
                }
            raise FFmpegExecutionError("FFmpeg binary is not found in PATH.")


        from herald.concurrency import get_semaphores

        job_log_prefix = f"Job '{job_id}': " if job_id else ""
        timeout_sec = getattr(settings, "HERALD_FFMPEG_TIMEOUT_SECONDS", 300)

        logger.info(f"{job_log_prefix}waiting for FFmpeg slot")
        with get_semaphores().ffmpeg:
            logger.info(f"{job_log_prefix}FFmpeg slot acquired")
            logger.info(f"{job_log_prefix}FFmpeg process starting")
            t_ffmpeg_proc_start = datetime.now(UTC)
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout_sec)
            except subprocess.TimeoutExpired as te:
                logger.error(f"{job_log_prefix}FFmpeg process timed out after {timeout_sec} seconds")
                if output_mp3_path.exists():
                    try:
                        output_mp3_path.unlink()
                    except Exception as e:
                        logger.warning(f"Failed to remove partial output '{output_mp3_path}' after timeout: {e}")
                raise FFmpegExecutionError(f"FFmpeg process timed out after {timeout_sec} seconds") from te

            ffmpeg_duration = (datetime.now(UTC) - t_ffmpeg_proc_start).total_seconds()

            if res.returncode != 0:
                if output_mp3_path.exists():
                    try:
                        output_mp3_path.unlink()
                    except Exception as e:
                        logger.warning(f"Failed to remove partial output '{output_mp3_path}' after failure: {e}")
                raise FFmpegExecutionError(f"FFmpeg failed with exit code {res.returncode}: {res.stderr}")

            logger.info(f"{job_log_prefix}FFmpeg process completed in {ffmpeg_duration:.2f} seconds")

        val_info = validate_audio_file(output_mp3_path)
        logger.info(f"{job_log_prefix}validation completed")
        embed_id3_metadata(output_mp3_path, title=episode_title, description=episode_description, job_id=job_id)

        checksum = compute_file_sha256(output_mp3_path)
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
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
