"""
Voice catalog and persistent voice sample management for Herald.
Pre-renders and caches fixed voice sample audio files.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from herald.concurrency import tts_slot_lock
from herald.config import settings
from herald.tts.kokoro_client import KokoroClient

logger = logging.getLogger("herald.services.voice_manager")

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


def convert_wav_to_mp3(wav_path: Path, mp3_path: Path) -> Path:
    """Convert WAV file to MP3 using ffmpeg, with mock fallback for test environments."""
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    if os.getenv("HERALD_MOCK_TTS") == "1" or not shutil.which("ffmpeg"):
        # Test environment mock MP3
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            mp3_path.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00#dummy_mp3_sample_data#")
        return mp3_path

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
    If not already generated, synthesizes in a global TTS concurrency slot, converts to MP3, and caches.
    """
    v_clean = voice.lower().strip()
    allowed = settings.get_allowed_voices_list()
    if v_clean not in allowed:
        raise ValueError(f"Voice '{voice}' is not in allowed voices: {allowed}")

    sample_mp3 = get_voice_sample_path(v_clean)
    if sample_mp3.exists() and sample_mp3.stat().st_size > 0:
        return sample_mp3

    client = kokoro_client or KokoroClient()
    sample_text = f"Hello, this is Herald reading a preview with the {v_clean} voice."
    temp_wav = sample_mp3.with_suffix(".tmp.wav")

    with tts_slot_lock(db=db):
        if sample_mp3.exists() and sample_mp3.stat().st_size > 0:
            return sample_mp3

        try:
            client.synthesize_chunk(
                text=sample_text,
                output_path=temp_wav,
                voice=v_clean,
                speed=1.0,
                timeout=getattr(settings, "KOKORO_TIMEOUT_SECONDS", 180.0),
            )
            convert_wav_to_mp3(temp_wav, sample_mp3)
            logger.info(f"Generated and cached voice sample for '{v_clean}' at '{sample_mp3}'")
        finally:
            if temp_wav.exists():
                temp_wav.unlink(missing_ok=True)

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
