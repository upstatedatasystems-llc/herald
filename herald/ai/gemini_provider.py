"""
Gemini AI Provider implementation with response caching, secure header auth, and full capabilities.
"""

import logging
import time
from typing import Any

import httpx

from herald.ai.base import AIProvider, ProviderCapabilities
from herald.ai.schema import PodcastScriptResponse
from herald.config import settings

logger = logging.getLogger("herald.ai.gemini")


def generate_podcast_script(*args, **kwargs):
    """Lazy wrapper for herald.gemini.client.generate_podcast_script to prevent circular imports."""
    from herald.gemini.client import generate_podcast_script as _gps
    return _gps(*args, **kwargs)


class GeminiProvider(AIProvider):
    """Gemini AI Provider implementation with response caching and secure header auth."""

    def __init__(self, cache_ttl_seconds: float = 300.0) -> None:
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cached_health: dict[str, Any] | None = None
        self._cache_timestamp: float = 0.0

    @property
    def provider_name(self) -> str:
        return "Gemini"

    @property
    def configured_model(self) -> str:
        return settings.GEMINI_MODEL

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            script_brief=True,
            script_standard=True,
            structured_output=True,
            research_grounding=True,
            usage_metrics=True,
        )

    def is_configured(self) -> bool:
        return bool(settings.GEMINI_API_KEY and settings.GEMINI_API_KEY.strip())

    def generate_script(
        self,
        source_text: str,
        request_mode: str = "standard",
        research_dossier: dict[str, Any] | None = None,
        source_title: str | None = None,
        job_id: str | None = None,
    ) -> PodcastScriptResponse:
        return generate_podcast_script(
            source_text=source_text,
            request_mode=request_mode,
            research_dossier=research_dossier,
            source_title=source_title,
            job_id=job_id,
        )

    def check_connection(self, timeout_seconds: float = 5.0, force_refresh: bool = False) -> dict[str, Any]:
        """
        Check Gemini API connectivity using a lightweight model info endpoint.
        Uses x-goog-api-key header and caches results for 5 minutes unless force_refresh is True.
        """
        now = time.time()
        if not force_refresh and self._cached_health and (now - self._cache_timestamp) < self.cache_ttl_seconds:
            return dict(self._cached_health)

        if not self.is_configured():
            res = {
                "provider": self.provider_name,
                "configured": False,
                "connected": False,
                "model": self.configured_model,
                "error": "API key not configured",
            }
            self._cached_health = res
            self._cache_timestamp = now
            return res

        key = settings.GEMINI_API_KEY.strip()
        model = self.configured_model.strip()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
        headers = {"x-goog-api-key": key}

        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                resp = client.get(url, headers=headers)

            if resp.status_code == 200:
                res = {
                    "provider": self.provider_name,
                    "configured": True,
                    "connected": True,
                    "model": model,
                    "error": None,
                }
            elif resp.status_code in (401, 403):
                res = {
                    "provider": self.provider_name,
                    "configured": True,
                    "connected": False,
                    "model": model,
                    "error": "authentication failed",
                }
            elif resp.status_code == 429:
                res = {
                    "provider": self.provider_name,
                    "configured": True,
                    "connected": False,
                    "model": model,
                    "error": "rate limit exceeded",
                }
            else:
                res = {
                    "provider": self.provider_name,
                    "configured": True,
                    "connected": False,
                    "model": model,
                    "error": f"API returned status {resp.status_code}",
                }
        except httpx.TimeoutException:
            res = {
                "provider": self.provider_name,
                "configured": True,
                "connected": False,
                "model": model,
                "error": "connection timed out",
            }
        except Exception as e:
            err_str = str(e)
            if key and key in err_str:
                err_str = err_str.replace(key, "[REDACTED]")
            res = {
                "provider": self.provider_name,
                "configured": True,
                "connected": False,
                "model": model,
                "error": f"network error: {err_str}",
            }

        self._cached_health = res
        self._cache_timestamp = now
        return res
