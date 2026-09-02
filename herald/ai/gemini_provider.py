import logging
from typing import Any
import httpx

from herald.ai.base import AIProvider
from herald.config import settings
from herald.gemini.client import (
    GeminiAuthError,
    GeminiError,
    generate_podcast_script,
)
from herald.gemini.schema import PodcastScriptResponse

logger = logging.getLogger("herald.ai.gemini")


class GeminiProvider(AIProvider):
    """Gemini AI Provider implementation."""

    @property
    def provider_name(self) -> str:
        return "Gemini"

    @property
    def configured_model(self) -> str:
        return settings.GEMINI_MODEL

    def is_configured(self) -> bool:
        return bool(settings.GEMINI_API_KEY and settings.GEMINI_API_KEY.strip())

    def generate_script(
        self,
        source_text: str,
        request_mode: str = "standard",
        research_dossier: dict[str, Any] | None = None,
        source_title: str | None = None,
    ) -> PodcastScriptResponse:
        if not self.is_configured():
            raise GeminiAuthError("Gemini API key is not configured.")
        return generate_podcast_script(
            source_text=source_text,
            request_mode=request_mode,
            research_dossier=research_dossier,
            source_title=source_title,
        )

    def check_connection(self, timeout_seconds: float = 5.0) -> dict[str, Any]:
        """
        Check Gemini API connectivity using a low-cost model info endpoint.
        Guarantees secrets are never leaked in error messages.
        """
        if not self.is_configured():
            return {
                "provider": self.provider_name,
                "configured": False,
                "connected": False,
                "model": self.configured_model,
                "error": "API key not configured",
            }

        key = settings.GEMINI_API_KEY.strip()
        model = self.configured_model.strip()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}?key={key}"

        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                resp = client.get(url)

            if resp.status_code == 200:
                return {
                    "provider": self.provider_name,
                    "configured": True,
                    "connected": True,
                    "model": model,
                    "error": None,
                }
            elif resp.status_code in (401, 403):
                return {
                    "provider": self.provider_name,
                    "configured": True,
                    "connected": False,
                    "model": model,
                    "error": "authentication failed",
                }
            elif resp.status_code == 429:
                return {
                    "provider": self.provider_name,
                    "configured": True,
                    "connected": False,
                    "model": model,
                    "error": "rate limit exceeded",
                }
            else:
                return {
                    "provider": self.provider_name,
                    "configured": True,
                    "connected": False,
                    "model": model,
                    "error": f"API returned status {resp.status_code}",
                }
        except httpx.TimeoutException:
            return {
                "provider": self.provider_name,
                "configured": True,
                "connected": False,
                "model": model,
                "error": "connection timed out",
            }
        except Exception as e:
            # Sanitize exception message to ensure key is not present
            err_str = str(e)
            if key and key in err_str:
                err_str = err_str.replace(key, "[REDACTED]")
            return {
                "provider": self.provider_name,
                "configured": True,
                "connected": False,
                "model": model,
                "error": f"network error: {err_str}",
            }
