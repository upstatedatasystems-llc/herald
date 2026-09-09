"""
Voice catalog and persistent voice sample management for Herald.
Pre-renders and caches fixed voice sample audio files.
"""

import json
import logging
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from herald.audio.ffmpeg_builder import validate_audio_file
from herald.concurrency import get_tts_slot_wait_timeout_seconds, tts_slot_lock
from herald.config import settings
from herald.tts.kokoro_client import KokoroClient

logger = logging.getLogger("herald.services.voice_manager")

# Standard fixed comparison text used across all voice previews
VOICE_SAMPLE_TEXT = "Hello, this is Herald reading your text with Kokoro TTS."

HERALD_VOICE_SAMPLE_CACHE_VERSION = "v1"

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
    force: bool = False,
    db: Session | None = None,
) -> Path:
    """
    Ensure standard voice sample MP3 exists on disk and is recorded in the manifest.
    If force=False and get_cached_voice_sample(voice) is valid, returns it immediately.
    Otherwise, removes stale/orphan preview, synthesizes in a global TTS concurrency slot,
    converts to MP3 atomically, validates audio, and updates manifest.
    """
    v_clean = voice.lower().strip()
    allowed = settings.get_allowed_voices_list()
    if v_clean not in allowed:
        raise ValueError(f"Voice '{voice}' is not in allowed voices: {allowed}")

    if not force:
        cached = get_cached_voice_sample(v_clean)
        if cached is not None:
            return cached

    sample_mp3 = get_voice_sample_path(v_clean)
    # Clean corrupt or stale cache file if present
    if sample_mp3.exists():
        sample_mp3.unlink(missing_ok=True)

    client = kokoro_client or KokoroClient()
    unique_suffix = uuid.uuid4().hex[:12]
    temp_wav = sample_mp3.with_name(f"{sample_mp3.stem}_{unique_suffix}.tmp.wav")
    temp_mp3 = sample_mp3.with_name(f"{sample_mp3.stem}_{unique_suffix}.tmp.mp3")

    synth_timeout = float(getattr(settings, "KOKORO_SYNTHESIS_TIMEOUT_SECONDS", 180.0))
    with tts_slot_lock(db=db, timeout_seconds=get_tts_slot_wait_timeout_seconds()):
        if not force:
            cached = get_cached_voice_sample(v_clean)
            if cached is not None:
                return cached

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

            # Update persistent versioned manifest
            try:
                manifest = load_voice_sample_manifest()
                manifest[v_clean] = {
                    "voice_id": v_clean,
                    "sample_text_hash": compute_sample_text_hash(),
                    "text_hash": compute_sample_text_hash(),
                    "speed": 1.0,
                    "format": "mp3",
                    "cache_version": HERALD_VOICE_SAMPLE_CACHE_VERSION,
                    "file_path": str(sample_mp3),
                    "generated_at": datetime.now(UTC).isoformat(),
                }
                save_voice_sample_manifest(manifest)
            except Exception as me:
                logger.debug(f"Failed to update voice sample manifest for '{v_clean}': {me}")
        finally:
            if temp_wav.exists():
                temp_wav.unlink(missing_ok=True)
            if temp_mp3.exists():
                temp_mp3.unlink(missing_ok=True)

    return sample_mp3


def get_voice_sample_manifest_path() -> Path:
    """Return path to voice samples cache manifest JSON."""
    return get_voice_samples_dir() / "manifest.json"


def load_voice_sample_manifest() -> dict[str, Any]:
    """Load cached voice sample manifest from disk."""
    p = get_voice_sample_manifest_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_voice_sample_manifest(manifest: dict[str, Any]) -> None:
    """Save voice sample manifest to disk atomically."""
    p = get_voice_sample_manifest_path()
    try:
        tmp_p = p.with_suffix(".tmp.json")
        tmp_p.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(tmp_p, p)
    except Exception as e:
        logger.warning(f"Failed to write voice sample manifest: {e}")


def compute_sample_text_hash(text: str = VOICE_SAMPLE_TEXT) -> str:
    """Compute deterministic short hash of the canonical preview sample text."""
    import hashlib

    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def get_cached_voice_sample(voice: str) -> Path | None:
    """
    Check if a voice preview sample is already prewarmed, recorded in manifest, and valid on disk.
    Returns Path if available, None on cache miss.
    Rejects orphan files not tracked in the manifest or with mismatched version/settings.
    """
    v_clean = voice.lower().strip()
    sample_mp3 = get_voice_sample_path(v_clean)
    if not is_valid_sample_audio(sample_mp3):
        return None

    manifest = load_voice_sample_manifest()
    entry = manifest.get(v_clean)
    if not entry or not isinstance(entry, dict):
        # Reject orphan files without valid manifest entry
        return None

    if entry.get("voice_id") != v_clean:
        return None
    curr_hash = compute_sample_text_hash()
    text_hash = entry.get("sample_text_hash") or entry.get("text_hash")
    if text_hash != curr_hash:
        return None
    if entry.get("speed") != 1.0:
        return None
    if entry.get("format") != "mp3":
        return None
    if entry.get("cache_version") != HERALD_VOICE_SAMPLE_CACHE_VERSION:
        return None

    return sample_mp3


def prewarm_all_voice_samples(
    kokoro_client: KokoroClient | None = None,
    force: bool = False,
    db: Session | None = None,
) -> dict[str, bool]:
    """
    Prewarm all allowed voice sample MP3s.
    Generates missing or outdated voice preview files into the voice sample cache.
    Returns mapping of voice_id -> success boolean.
    """
    allowed = settings.get_allowed_voices_list()
    results: dict[str, bool] = {}
    client = kokoro_client or KokoroClient()

    for v in allowed:
        try:
            cached = get_cached_voice_sample(v)
            if cached and not force:
                logger.info(f"Voice sample for '{v}' already prewarmed: {cached}")
                results[v] = True
                continue

            logger.info(f"Prewarming voice sample for '{v}' (force={force})...")
            ensure_voice_sample(voice=v, kokoro_client=client, force=force, db=db)
            results[v] = True
        except Exception as e:
            logger.error(f"Failed to prewarm voice sample for '{v}': {e}")
            results[v] = False

    return results


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


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Herald Voice Prewarm CLI")
    parser.add_argument("--prewarm", action="store_true", help="Prewarm all voice samples")
    parser.add_argument(
        "--force", action="store_true", help="Force regenerate existing voice samples"
    )
    args = parser.parse_args()

    if args.prewarm:
        import sys

        print("Prewarming all Herald voice preview samples...")
        res = prewarm_all_voice_samples(force=args.force)
        all_ok = True
        for vid, ok in res.items():
            status_str = "SUCCESS" if ok else "FAILED"
            print(f"  {vid}: {status_str}")
            if not ok:
                all_ok = False
        if not all_ok:
            sys.exit(1)
        sys.exit(0)
