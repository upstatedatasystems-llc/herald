import logging
import os
import shutil
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

    def __init__(
        self,
        base_url: str | None = None,
        voice: str | None = None,
        speed: float | None = None,
    ):
        self.base_url = (base_url or settings.KOKORO_BASE_URL).rstrip("/")
        self.voice = voice or settings.KOKORO_VOICE
        self.speed = speed or settings.KOKORO_SPEED

    def health_check(self) -> dict[str, Any]:
        """
        Verify Kokoro container accessibility (/v1/models), FFmpeg availability, and test inference status.
        Supports bounded grace period during active inference saturation.
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
            if last_good and (now - last_good).total_seconds() <= grace_seconds:
                logger.info(
                    f"Kokoro probe timed out during active inference window ({e}), returning degraded healthy state (last successful: {last_good.isoformat()})"
                )
                status["kokoro_api"] = True
                status["degraded"] = True
            else:
                logger.warning(f"Kokoro probe timed out and grace period expired ({e})")
                status["error"] = f"Kokoro probe timeout: {e}"
        except Exception as e:
            logger.warning(f"Kokoro API endpoint '{self.base_url}' health check failed: {e}")
            status["error"] = str(e)

        if status["ffmpeg"] and (status["kokoro_api"] or os.environ.get("HERALD_MOCK_TTS") == "1"):
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
