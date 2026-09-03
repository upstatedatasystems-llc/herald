"""
Voice catalog and persistent voice sample management for Herald.
Pre-renders and caches fixed voice sample audio files.
"""

import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from herald.audio.ffmpeg_builder import validate_audio_file
from herald.concurrency import tts_slot_lock
from herald.config import settings
from herald.tts.kokoro_client import KokoroClient

logger = logging.getLogger("herald.services.voice_manager")

# Standard fixed comparison text used across all voice previews
VOICE_SAMPLE_TEXT = "Hello, this is Herald reading your text with Kokoro TTS."

VOICE_METADATA: dict[str, dict[str, str]] = {
    "af_heart": {
        "display_name": "Heart",
        "gender": "Female (US)",
        "description": "Warm, natural, default narrator voice",
    },
    "af_bella": {
        "display_name": "Bella",
        "gender": "Female (US)",
        "description": "Clear, expressive, dynamic",
    },
    "af_sarah": {
        "display_name": "Sarah",
        "gender": "Female (US)",
        "description": "Bright, articulate, modern",
    },
    "am_adam": {
        "display_name": "Adam",
        "gender": "Male (US)",
        "description": "Deep, calm, authoritative",
    },
    "am_michael": {
        "display_name": "Michael",
        "gender": "Male (US)",
        "description": "Smooth, professional, balanced",
    },
}


def get_voice_samples_dir() -> Path:
    """Return directory where persistent voice sample MP3s are stored."""
    base_dir = Path(getattr(settings, "HERALD_WORK_DIR", "/tmp/herald"))
    samples_dir = base_dir / "voice_samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    return samples_dir


def get_voice_sample_path(voice: str) -> Path:
    """Return standard persistent path for a voice sample MP3."""
    v_clean = voice.lower().strip()
    return get_voice_samples_dir() / f"sample_{v_clean}.mp3"


def is_valid_sample_audio(path: Path) -> bool:
    """Verify that a cached audio sample exists, is non-empty, and represents valid audio."""
    if not path.exists() or path.stat().st_size < 10:
        return False
    if os.getenv("HERALD_MOCK_TTS") == "1":
        return True
    try:
        meta = validate_audio_file(path)
        return bool(meta and meta.get("size_bytes", 0) > 0)
    except Exception:
        return False


def convert_wav_to_mp3(wav_path: Path, mp3_path: Path) -> Path:
    """
    Convert WAV file to MP3 using ffmpeg.
    Dummy fallback is strictly limited to explicit HERALD_MOCK_TTS=1 test environments.
    """
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    if os.getenv("HERALD_MOCK_TTS") == "1":
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            mp3_path.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00#dummy_mp3_sample_data#")
        return mp3_path

    if not shutil.which("ffmpeg"):
        logger.error("FFmpeg executable not found on PATH in production runtime.")
        raise RuntimeError("FFmpeg executable not found on PATH.")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(wav_path),
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(mp3_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.error(f"FFmpeg MP3 conversion failed: {proc.stderr}")
        raise RuntimeError(f"FFmpeg conversion failed: {proc.stderr}")

    return mp3_path


def ensure_voice_sample(
    voice: str,
    kokoro_client: KokoroClient | None = None,
    db: Session | None = None,
) -> Path:
    """
    Ensure standard voice sample MP3 exists on disk.
    If not already generated or corrupt, synthesizes in a global TTS concurrency slot, converts to MP3 atomically, and caches.
    """
    v_clean = voice.lower().strip()
    allowed = settings.get_allowed_voices_list()
    if v_clean not in allowed:
        raise ValueError(f"Voice '{voice}' is not in allowed voices: {allowed}")

    sample_mp3 = get_voice_sample_path(v_clean)
    if is_valid_sample_audio(sample_mp3):
        return sample_mp3

    # Clean corrupt cache file if present
    if sample_mp3.exists():
        sample_mp3.unlink(missing_ok=True)

    client = kokoro_client or KokoroClient()
    unique_suffix = uuid.uuid4().hex[:12]
    temp_wav = sample_mp3.with_name(f"{sample_mp3.stem}_{unique_suffix}.tmp.wav")
    temp_mp3 = sample_mp3.with_name(f"{sample_mp3.stem}_{unique_suffix}.tmp.mp3")

    synth_timeout = float(getattr(settings, "KOKORO_SYNTHESIS_TIMEOUT_SECONDS", 180.0))
    with tts_slot_lock(db=db, timeout_seconds=synth_timeout):
        if is_valid_sample_audio(sample_mp3):
            return sample_mp3

        try:
            client.synthesize_chunk(
                text=VOICE_SAMPLE_TEXT,
                output_path=temp_wav,
                voice=v_clean,
                speed=1.0,
                timeout=synth_timeout,
            )
            convert_wav_to_mp3(temp_wav, temp_mp3)
            if not is_valid_sample_audio(temp_mp3):
                raise RuntimeError(
                    f"Synthesized voice sample for '{v_clean}' failed audio validation."
                )

            os.replace(temp_mp3, sample_mp3)
            logger.info(f"Generated and cached voice sample for '{v_clean}' at '{sample_mp3}'")
        finally:
            if temp_wav.exists():
                temp_wav.unlink(missing_ok=True)
            if temp_mp3.exists():
                temp_mp3.unlink(missing_ok=True)

    return sample_mp3


def get_all_voice_metadata() -> list[dict[str, Any]]:
    """Return ordered list of allowed voice metadata for browser display."""
    allowed = settings.get_allowed_voices_list()
    results = []
    for v in allowed:
        meta = VOICE_METADATA.get(
            v,
            {
                "display_name": v.capitalize(),
                "gender": "Unknown",
                "description": "Kokoro voice",
            },
        )
        results.append({"voice_id": v, **meta})
    return results
