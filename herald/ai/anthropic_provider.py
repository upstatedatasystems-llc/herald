"""
Anthropic Claude AI Provider Implementation for Herald.
Generates structured podcast scripts using Anthropic Messages API.
"""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from herald.ai.base import AIProvider, load_system_prompt
from herald.ai.errors import (
    AIAuthFailedError,
    AIClientTimeoutError,
    AIPermissionDeniedError,
    AIProviderError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
    AIRateLimitedError,
    AIRequestTooLargeError,
    AISchemaInvalidError,
)
from herald.ai.schema import PodcastScriptResponse
from herald.config import settings
from herald.services.ai_recorder import record_ai_interaction
from herald.services.redaction import sanitize_error

logger = logging.getLogger("herald.ai.anthropic")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"


def _extract_json_block(text: str) -> dict[str, Any]:
    """Extract JSON object from markdown code fences or raw text."""
    clean = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    # Fallback to direct json parse
    return json.loads(clean)


class AnthropicProvider(AIProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key or settings.ANTHROPIC_API_KEY
        self._model = model or settings.ANTHROPIC_MODEL or "claude-3-7-sonnet-20250219"

    @property
    def provider_name(self) -> str:
        return "Anthropic"

    @property
    def configured_model(self) -> str:
        return self._model

    def is_configured(self) -> bool:
        return bool(self._api_key and self._api_key.strip())

    def check_connection(self, timeout_seconds: float = 5.0, force_refresh: bool = False) -> dict[str, Any]:
        if not self.is_configured():
            return {
                "provider": self.provider_name,
                "configured": False,
                "connected": False,
                "model": self.configured_model,
                "error": "Anthropic API key is not configured.",
            }

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": self._model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }

        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                resp = client.post(ANTHROPIC_API_URL, json=payload, headers=headers)
            if resp.status_code == 200:
                return {
                    "provider": self.provider_name,
                    "configured": True,
                    "connected": True,
                    "model": self.configured_model,
                    "error": None,
                }
            if resp.status_code in (401, 403):
                err_msg = "authentication failed"
            elif resp.status_code == 404:
                err_msg = "configured model unavailable"
            elif resp.status_code == 429:
                err_msg = "rate limit exceeded"
            elif resp.status_code >= 500:
                err_msg = "provider unavailable"
            else:
                err_msg = f"HTTP error {resp.status_code}"
            return {
                "provider": self.provider_name,
                "configured": True,
                "connected": False,
                "model": self.configured_model,
                "error": err_msg,
            }
        except httpx.TimeoutException:
            return {
                "provider": self.provider_name,
                "configured": True,
                "connected": False,
                "model": self.configured_model,
                "error": "connection timed out",
            }
        except Exception as e:
            from herald.services.redaction import sanitize_error
            _, safe_err = sanitize_error(e)
            return {
                "provider": self.provider_name,
                "configured": True,
                "connected": False,
                "model": self.configured_model,
                "error": f"network error: {safe_err}",
            }

    def generate_script(
        self,
        source_text: str,
        request_mode: str = "standard",
        research_dossier: dict[str, Any] | None = None,
        source_title: str | None = None,
        job_id: str | None = None,
    ) -> PodcastScriptResponse:
        if not self.is_configured():
            raise RuntimeError("Anthropic API key is not configured.")

        mode_clean = (request_mode or "standard").lower()
        system_prompt = load_system_prompt()
        json_instruction = (
            "\n\nIMPORTANT: Return ONLY a single valid JSON object strictly matching the schema: "
            '{"episode_title": str, "episode_description": str, "estimated_minutes": int, '
            '"source_title": str, "segments": [{"order": int, "heading": str, "narration": str}], "warnings": [str]}. '
            "Do not include commentary outside the JSON block."
        )

        user_content = f"""
REQUESTED MODE: {mode_clean.upper()}
SOURCE TITLE: {source_title or 'N/A'}

<SOURCE_DATA>
{source_text}
</SOURCE_DATA>

Generate the podcast script JSON response adhering to spoken prose rules now.
"""

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

        attempt = 1
        t0 = datetime.now(UTC)
        payload = {
            "model": self._model,
            "max_tokens": 8192,
            "system": f"{system_prompt}{json_instruction}",
            "messages": [{"role": "user", "content": user_content}],
        }

        try:
            from herald.concurrency import get_semaphores
            with get_semaphores().script, httpx.Client(timeout=settings.effective_ai_timeout_seconds) as client:
                resp = client.post(ANTHROPIC_API_URL, json=payload, headers=headers)
        except httpx.TimeoutException as timeout_err:
            record_ai_interaction(
                job_id=job_id,
                provider="anthropic",
                model=self._model,
                operation="script_generation",
                attempt=attempt,
                started_at=t0,
                completed_at=datetime.now(UTC),
                success=False,
                error=timeout_err,
                metadata={"attempt": attempt, "mode": mode_clean, "timeout_seconds": settings.effective_ai_timeout_seconds},
            )
            raise AIClientTimeoutError(
                f"Anthropic client timeout connecting to {self._model}",
                provider="anthropic",
                model=self._model,
                operation="script_generation",
            )
        except Exception as net_err:
            record_ai_interaction(
                job_id=job_id,
                provider="anthropic",
                model=self._model,
                operation="script_generation",
                attempt=attempt,
                started_at=t0,
                completed_at=datetime.now(UTC),
                success=False,
                error=net_err,
                metadata={"attempt": attempt, "mode": mode_clean},
            )
            _, safe_net_err = sanitize_error(net_err)
            raise AIProviderError(
                f"Anthropic network failure: {safe_net_err}",
                provider="anthropic",
                model=self._model,
                operation="script_generation",
            )

        if resp.status_code != 200:
            record_ai_interaction(
                job_id=job_id,
                provider="anthropic",
                model=self._model,
                operation="script_generation",
                attempt=attempt,
                http_status=resp.status_code,
                started_at=t0,
                completed_at=datetime.now(UTC),
                success=False,
                error=f"HTTP {resp.status_code}: {resp.text[:300]}",
                metadata={"attempt": attempt, "mode": mode_clean},
            )
            if resp.status_code == 401:
                raise AIAuthFailedError("Anthropic authentication failed: HTTP 401", provider="anthropic", model=self._model, http_status=401)
            if resp.status_code == 403:
                raise AIPermissionDeniedError("Anthropic permission denied: HTTP 403", provider="anthropic", model=self._model, http_status=403)
            if resp.status_code == 408:
                raise AIProviderTimeoutError("Anthropic provider timeout: HTTP 408", provider="anthropic", model=self._model, http_status=408)
            if resp.status_code == 413:
                raise AIRequestTooLargeError("Anthropic request payload too large: HTTP 413", provider="anthropic", model=self._model, http_status=413)
            if resp.status_code == 429:
                retry_h = resp.headers.get("retry-after")
                retry_s = float(retry_h) if (retry_h and retry_h.isdigit()) else None
                raise AIRateLimitedError("Anthropic rate limited: HTTP 429", provider="anthropic", model=self._model, http_status=429, retry_after_seconds=retry_s)
            if resp.status_code >= 500:
                raise AIProviderUnavailableError(f"Anthropic API returned HTTP {resp.status_code}", provider="anthropic", model=self._model, http_status=resp.status_code)
            raise AIProviderError(f"Anthropic API error ({resp.status_code}): {resp.text[:300]}", provider="anthropic", model=self._model, http_status=resp.status_code)

        result_json = resp.json()
        content_blocks = result_json.get("content", [])
        text_response = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")

        usage = result_json.get("usage", {})
        p_tok = usage.get("input_tokens")
        c_tok = usage.get("output_tokens")
        t_tok = (p_tok + c_tok) if (p_tok is not None and c_tok is not None) else None

        try:
            script_dict = _extract_json_block(text_response)
            parsed_script = PodcastScriptResponse(**script_dict)

            record_ai_interaction(
                job_id=job_id,
                provider="anthropic",
                model=self._model,
                operation="script_generation",
                attempt=attempt,
                http_status=resp.status_code,
                started_at=t0,
                completed_at=datetime.now(UTC),
                success=True,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=t_tok,
                metadata={"attempt": attempt, "mode": mode_clean},
            )
            return parsed_script
        except Exception as parse_err:
            record_ai_interaction(
                job_id=job_id,
                provider="anthropic",
                model=self._model,
                operation="script_generation",
                attempt=attempt,
                http_status=resp.status_code,
                started_at=t0,
                completed_at=datetime.now(UTC),
                success=False,
                error=parse_err,
                metadata={"attempt": attempt, "mode": mode_clean},
            )
            raise AISchemaInvalidError(
                f"Anthropic response schema invalid: {parse_err}",
                provider="anthropic",
                model=self._model,
            )
