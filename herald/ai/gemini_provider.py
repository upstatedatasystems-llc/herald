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


def generate_grounded_research(*args, **kwargs):
    from herald.gemini.client import generate_grounded_research as _ggr
    return _ggr(*args, **kwargs)


def normalize_research_dossier(*args, **kwargs):
    from herald.gemini.client import normalize_research_dossier as _nrd
    return _nrd(*args, **kwargs)


def audit_research_script(*args, **kwargs):
    from herald.gemini.client import audit_research_script as _ars
    return _ars(*args, **kwargs)


def repair_research_script(*args, **kwargs):
    from herald.gemini.client import repair_research_script as _rrs
    return _rrs(*args, **kwargs)


def audit_script_fidelity(*args, **kwargs):
    from herald.gemini.client import audit_script_fidelity as _asf
    return _asf(*args, **kwargs)


def repair_script_fidelity(*args, **kwargs):
    from herald.gemini.client import repair_script_fidelity as _rsf
    return _rsf(*args, **kwargs)


def extract_article_via_url_context(*args, **kwargs):
    from herald.gemini.client import extract_article_via_url_context as _eau
    return _eau(*args, **kwargs)


class GeminiProvider(AIProvider):
    """Gemini AI Provider implementation with response caching and secure header auth."""

    def __init__(self, model: str | None = None, model_name: str | None = None, cache_ttl_seconds: float = 300.0) -> None:
        self._model = model or model_name or settings.GEMINI_MODEL
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cached_health: dict[str, Any] | None = None
        self._cache_timestamp: float = 0.0

    @property
    def provider_name(self) -> str:
        return "Gemini"

    @property
    def configured_model(self) -> str:
        return self._model

    @property
    def research_model(self) -> str:
        return settings.GEMINI_RESEARCH_MODEL

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
            model_name=self.configured_model,
        )

    def generate_grounded_research(
        self,
        source_text: str,
        research_depth: str = "medium",
        job_id: str | None = None,
    ) -> dict[str, Any]:
        from herald.gemini.client import generate_grounded_research as _ggr
        return _ggr(
            source_text=source_text,
            research_depth=research_depth,
            model_name=self.research_model,
            job_id=job_id,
        )

    def normalize_research_dossier(
        self,
        source_text: str,
        grounded_research_data: dict[str, Any] | None = None,
        job_id: str | None = None,
    ) -> Any:
        from herald.gemini.client import normalize_research_dossier as _nrd
        return _nrd(
            source_text=source_text,
            grounded_research_data=grounded_research_data,
            model_name=self.configured_model,
            job_id=job_id,
        )

    def audit_research_script(
        self,
        source_text: str,
        research_dossier: dict[str, Any] | None,
        script_dict: dict[str, Any] | None,
        job_id: str | None = None,
    ) -> Any:
        from herald.gemini.client import audit_research_script as _ars
        return _ars(
            source_text=source_text,
            research_dossier=research_dossier,
            script_dict=script_dict,
            model_name=self.configured_model,
            job_id=job_id,
        )

    def repair_research_script(
        self,
        source_text: str,
        research_dossier: dict[str, Any] | None,
        script_dict: dict[str, Any] | None,
        audit_result: dict[str, Any] | None,
        job_id: str | None = None,
    ) -> Any:
        from herald.gemini.client import repair_research_script as _rrs
        return _rrs(
            source_text=source_text,
            research_dossier=research_dossier,
            script_dict=script_dict,
            audit_result=audit_result,
            model_name=self.configured_model,
            job_id=job_id,
        )

    def audit_script_fidelity(
        self,
        source_text: str,
        script_dict: dict[str, Any] | None,
        job_id: str | None = None,
    ) -> Any:
        from herald.gemini.client import audit_script_fidelity as _asf
        return _asf(
            source_text=source_text,
            script_dict=script_dict,
            model_name=self.configured_model,
            job_id=job_id,
        )

    def repair_script_fidelity(
        self,
        source_text: str,
        script_dict: dict[str, Any] | None,
        audit_result: dict[str, Any] | None,
        job_id: str | None = None,
    ) -> Any:
        from herald.gemini.client import repair_script_fidelity as _rsf
        return _rsf(
            source_text=source_text,
            script_dict=script_dict,
            audit_result=audit_result,
            model_name=self.configured_model,
            job_id=job_id,
        )

    def extract_article_via_url_context(
        self,
        url: str,
        job_id: str | None = None,
    ) -> dict[str, Any] | None:
        from herald.gemini.client import extract_article_via_url_context as _eavuc
        return _eavuc(
            url=url,
            model_name=self.configured_model,
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
            elif resp.status_code == 404:
                res = {
                    "provider": self.provider_name,
                    "configured": True,
                    "connected": False,
                    "model": model,
                    "error": f"model '{model}' unavailable or not found (404)",
                    "error_category": "AI_MODEL_UNAVAILABLE",
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

    def check_research_connection(
        self, timeout_seconds: float = 5.0, force_refresh: bool = False
    ) -> dict[str, Any]:
        """
        Check Gemini Grounded Research model connectivity independently.
        Validates GEMINI_RESEARCH_MODEL availability and Google Search Grounding readiness.
        """
        if not self.is_configured():
            return {
                "provider": "Gemini Research",
                "configured": False,
                "connected": False,
                "model": self.research_model,
                "error": "API key not configured",
            }

        key = settings.GEMINI_API_KEY.strip()
        model = self.research_model.strip()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": key}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"maxOutputTokens": 5},
        }

        try:
            from herald.gemini.client import _is_gemini_model_not_found_response

            with httpx.Client(timeout=timeout_seconds) as client:
                resp = client.post(url, json=payload, headers=headers)

            if resp.status_code == 200:
                return {
                    "provider": "Gemini Research",
                    "configured": True,
                    "connected": True,
                    "model": model,
                    "error": None,
                }
            elif resp.status_code == 404:
                is_unavail, err_msg = _is_gemini_model_not_found_response(resp)
                if is_unavail:
                    return {
                        "provider": "Gemini Research",
                        "configured": True,
                        "connected": False,
                        "model": model,
                        "error": f"research model '{model}' unavailable or not found (404): {err_msg}",
                        "error_category": "AI_MODEL_UNAVAILABLE",
                    }
                else:
                    return {
                        "provider": "Gemini Research",
                        "configured": True,
                        "connected": False,
                        "model": model,
                        "error": f"HTTP 404 error: {err_msg}",
                        "error_category": "AI_SERVER_ERROR",
                    }
            elif resp.status_code in (401, 403):
                return {
                    "provider": "Gemini Research",
                    "configured": True,
                    "connected": False,
                    "model": model,
                    "error": "authentication failed",
                }
            elif resp.status_code == 429:
                return {
                    "provider": "Gemini Research",
                    "configured": True,
                    "connected": False,
                    "model": model,
                    "error": "rate limit exceeded",
                }
            elif resp.status_code == 400:
                return {
                    "provider": "Gemini Research",
                    "configured": True,
                    "connected": False,
                    "model": model,
                    "error": f"grounding tool failure: {resp.text}",
                }
            else:
                return {
                    "provider": "Gemini Research",
                    "configured": True,
                    "connected": False,
                    "model": model,
                    "error": f"API returned status {resp.status_code}",
                }
        except httpx.TimeoutException:
            return {
                "provider": "Gemini Research",
                "configured": True,
                "connected": False,
                "model": model,
                "error": "connection timed out",
            }
        except Exception as e:
            err_str = str(e)
            if key and key in err_str:
                err_str = err_str.replace(key, "[REDACTED]")
            return {
                "provider": "Gemini Research",
                "configured": True,
                "connected": False,
                "model": model,
                "error": f"network error: {err_str}",
            }
