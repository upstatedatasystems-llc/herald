import logging
import os
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from herald.config import settings
from herald.tts.base import BaseTTSEngine

logger = logging.getLogger("herald.tts.kokoro")


class KokoroTTSError(Exception):
    """Exception raised when Kokoro TTS synthesis fails."""


class KokoroTTSTimeoutError(KokoroTTSError):
    """Exception raised specifically when Kokoro synthesis HTTP request times out."""


class KokoroClient(BaseTTSEngine):
    """
    Kokoro-FastAPI engine client over internal OpenAI-compatible speech endpoint.
    """
    _last_successful_probe_at: datetime | None = None
    _active_syntheses: int = 0
    _active_syntheses_lock = threading.Lock()

    def __init__(
        self,
        base_url: str | None = None,
        voice: str | None = None,
        speed: float | None = None,
    ):
        self.base_url = (base_url or settings.KOKORO_BASE_URL).rstrip("/")
        self.voice = voice or settings.KOKORO_VOICE
        self.speed = speed or settings.KOKORO_SPEED

    @classmethod
    def is_synthesizing(cls) -> bool:
        """Check if any TTS synthesis is actively executing."""
        with cls._active_syntheses_lock:
            if cls._active_syntheses > 0:
                return True
        try:
            from herald.concurrency import get_semaphores
            sem = get_semaphores().global_tts
            if hasattr(sem, "_value") and sem._value == 0:
                return True
        except Exception:
            pass
        return False

    def health_check(self, busy_hint: bool = False) -> dict[str, Any]:
        """
        Verify Kokoro container accessibility (/v1/models), FFmpeg availability, and test inference status.
        Supports load-aware busy/degraded state during active synthesis and bounded grace period.
        Accepts cross-process busy_hint (e.g. from DB SYNTHESIZING jobs or PostgreSQL advisory lock).
        """
        status = {
            "healthy": False,
            "kokoro_api": False,
            "degraded": False,
            "ffmpeg": False,
            "model_path_exists": False,
            "error": None,
        }

        # Check FFmpeg
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            status["ffmpeg"] = True
        else:
            status["error"] = "FFmpeg binary is not found in PATH"

        # Check model host path if local
        model_path = Path(settings.KOKORO_MODEL_PATH)
        if model_path.exists():
            status["model_path_exists"] = True

        # Probe /v1/models directly (v0.7.1 API)
        now = datetime.now(UTC)
        grace_seconds = getattr(settings, "KOKORO_HEALTH_GRACE_SECONDS", 120)

        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"{self.base_url}/models")
                if resp.status_code == 200:
                    status["kokoro_api"] = True
                    KokoroClient._last_successful_probe_at = now
                else:
                    status["error"] = f"Kokoro /v1/models probe returned HTTP {resp.status_code}"
        except (httpx.TimeoutException, httpx.ConnectTimeout) as e:
            last_good = KokoroClient._last_successful_probe_at
            active = KokoroClient.is_synthesizing() or busy_hint
            # If probe times out while active synthesis is in progress or within grace period:
            if active or (last_good and (now - last_good).total_seconds() <= grace_seconds):
                logger.info(
                    f"Kokoro probe timed out during active inference window (active={active}, busy_hint={busy_hint}, {e}), "
                    f"returning degraded healthy state (last successful: {last_good.isoformat() if last_good else 'none'})"
                )
                status["kokoro_api"] = True
                status["degraded"] = True
            else:
                logger.warning(f"Kokoro probe timed out and grace period expired (active=False, busy_hint=False, {e})")
                status["error"] = f"Kokoro probe timeout: {e}"
        except httpx.ConnectError as e:
            # True connection error (refused, no route) -> hard down even if busy_hint is True
            logger.warning(f"Kokoro API endpoint '{self.base_url}' connection refused/failed: {e}")
            status["error"] = f"Kokoro connection failed: {e}"
        except Exception as e:
            logger.warning(f"Kokoro API endpoint '{self.base_url}' health check failed: {e}")
            status["error"] = str(e)

        if status["ffmpeg"] and not status["error"] and (status["kokoro_api"] or os.environ.get("HERALD_MOCK_TTS") == "1"):
            status["healthy"] = True

        return status

    def synthesize_chunk(
        self,
        text: str,
        output_path: Path,
        voice: str | None = None,
        speed: float | None = None,
        timeout: float | None = None,
    ) -> Path:
        """
        Synthesize text chunk to audio output file via OpenAI-compatible endpoint.
        """
        use_voice = voice or self.voice
        use_speed = speed if speed is not None else self.speed
        synthesis_timeout = timeout if timeout is not None else getattr(settings, "KOKORO_SYNTHESIS_TIMEOUT_SECONDS", 180.0)

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Mock mode fallback for local CI or testing without model weights
        if os.environ.get("HERALD_MOCK_TTS") == "1":
            logger.info(f"[MOCK TTS] Generating dummy silent WAV file for chunk: '{text[:30]}...'")
            import struct
            import wave

            sample_rate = settings.AUDIO_SAMPLE_RATE
            duration = max(1.0, len(text) / 15.0)  # Approx 15 chars per sec
            num_samples = int(sample_rate * duration)

            with wave.open(str(output_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                data = struct.pack("<" + ("h" * num_samples), *([0] * num_samples))
                wav_file.writeframes(data)
            return output_path

        endpoint = f"{self.base_url}/audio/speech"
        payload = {
            "model": "kokoro",
            "input": text,
            "voice": use_voice,
            "response_format": "wav",
            "speed": use_speed,
        }

        import time
        start_time = time.monotonic()

        with KokoroClient._active_syntheses_lock:
            KokoroClient._active_syntheses += 1

        try:
            logger.info(f"Synthesizing chunk ({len(text)} chars) with Kokoro voice '{use_voice}' (Timeout: {synthesis_timeout}s)")
            with httpx.Client(timeout=synthesis_timeout) as client:
                response = client.post(endpoint, json=payload)

            elapsed = time.monotonic() - start_time

            if response.status_code != 200:
                if output_path.exists():
                    output_path.unlink(missing_ok=True)
                raise KokoroTTSError(
                    f"Kokoro API error ({response.status_code}): {response.text}"
                )

            with open(output_path, "wb") as f:
                f.write(response.content)

            if output_path.stat().st_size == 0:
                if output_path.exists():
                    output_path.unlink(missing_ok=True)
                raise KokoroTTSError("Generated audio chunk file is 0 bytes")

            logger.info(f"Kokoro synthesis completed in {elapsed:.1f}s for {len(text)} chars")
            return output_path

        except (httpx.TimeoutException, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            elapsed = time.monotonic() - start_time
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            raise KokoroTTSTimeoutError(
                f"Kokoro synthesis timed out after {elapsed:.1f}s (configured timeout: {synthesis_timeout}s): {e}"
            )
        except Exception as e:
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            if isinstance(e, KokoroTTSError):
                raise
            raise KokoroTTSError(f"Kokoro synthesis failed: {e}")
        finally:
            with KokoroClient._active_syntheses_lock:
                KokoroClient._active_syntheses -= 1


def is_tts_actively_synthesizing(db: Any | None = None) -> bool:
    """
    Check if Kokoro TTS is actively synthesizing across processes.
    Checks:
    1. In-process active syntheses counter (KokoroClient.is_synthesizing()).
    2. Active database job status (PodcastJob.status == JobState.SYNTHESIZING).
    3. PostgreSQL advisory lock for TTS slot if on PostgreSQL.
    """
    if KokoroClient.is_synthesizing():
        return True

    try:
        from herald.db.models import JobState, PodcastJob

        if db is not None:
            active_job = db.query(PodcastJob).filter(
                PodcastJob.status == JobState.SYNTHESIZING.value
            ).first()
            if active_job is not None:
                return True

            # Check Postgres advisory lock
            try:
                bind = db.get_bind()
                if bind and getattr(bind.dialect, "name", "") == "postgresql":
                    from sqlalchemy import text as sa_text
                    from herald.config import settings
                    from herald.concurrency import TTS_ADVISORY_SLOT_BASE, get_effective_tts_global_slots
                    base_key = getattr(settings, "HERALD_TTS_SLOT_BASE", TTS_ADVISORY_SLOT_BASE)
                    num_slots = get_effective_tts_global_slots()
                    max_key = base_key + num_slots - 1
                    query = sa_text(
                        "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND (objid BETWEEN :base_key AND :max_key OR classid BETWEEN :base_key AND :max_key) LIMIT 1"
                    )
                    res = db.execute(query, {"base_key": base_key, "max_key": max_key}).scalar()
                    if res:
                        return True
            except Exception as lock_err:
                logger.debug(f"Error checking pg_locks for TTS advisory locks: {lock_err}")
        else:
            from herald.db.connection import get_db
            with get_db() as session:
                active_job = session.query(PodcastJob).filter(
                    PodcastJob.status == JobState.SYNTHESIZING.value
                ).first()
                if active_job is not None:
                    return True
    except Exception as e:
        logger.debug(f"Error checking cross-process synthesis state: {e}")

    return False

