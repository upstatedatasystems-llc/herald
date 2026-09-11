"""
Ollama Local AI Provider Implementation for Herald.
Generates structured podcast scripts using local Ollama JSON chat/generate API.
"""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from herald.ai.base import AIProvider, load_system_prompt
from herald.ai.errors import (
    AIClientTimeoutError,
    AIModelUnavailableError,
    AIProviderError,
    AIProviderUnavailableError,
    AISchemaInvalidError,
)
from herald.ai.schema import PodcastScriptResponse
from herald.config import settings
from herald.services.ai_recorder import record_ai_interaction
from herald.services.redaction import sanitize_error

logger = logging.getLogger("herald.ai.ollama")


def _extract_json_block(text: str) -> dict[str, Any]:
    """Extract JSON object from text."""
    clean = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return json.loads(clean)


class OllamaProvider(AIProvider):
    def __init__(self, base_url: str | None = None, model: str | None = None):
        self._base_url = (base_url or settings.OLLAMA_BASE_URL or "http://localhost:11434").rstrip("/")
        self._model = model or settings.OLLAMA_MODEL or "llama3.2"

    @property
    def provider_name(self) -> str:
        return "Ollama"

    @property
    def configured_model(self) -> str:
        return self._model

    def is_configured(self) -> bool:
        return bool(self._base_url and self._base_url.strip())

    def check_connection(self, timeout_seconds: float = 5.0, force_refresh: bool = False) -> dict[str, Any]:
        if not self.is_configured():
            return {
                "provider": self.provider_name,
                "configured": False,
                "connected": False,
                "model": self.configured_model,
                "error": "Ollama base URL is not configured.",
            }

        url = f"{self._base_url}/api/tags"
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                resp = client.get(url)
            if resp.status_code == 200:
                tags = resp.json().get("models", [])
                model_names = [m.get("name", "") for m in tags]
                # Check if configured model is available (or substring match)
                has_model = any(self._model.lower() in m.lower() for m in model_names)
                return {
                    "provider": self.provider_name,
                    "configured": True,
                    "connected": True,
                    "model": self.configured_model,
                    "models_available": len(tags),
                    "model_installed": has_model,
                    "error": None,
                }
            if resp.status_code == 404:
                err_msg = "configured model unavailable"
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
            raise RuntimeError("Ollama base URL is not configured.")

        mode_clean = (request_mode or "standard").lower()
        system_prompt = load_system_prompt()
        json_instruction = (
            "\n\nOutput Schema: Return ONLY a JSON object with keys: "
            '"episode_title" (str), "episode_description" (str), "estimated_minutes" (int), '
            '"source_title" (str), "segments" (array of {"order": int, "heading": str, "narration": str}), "warnings" (array of str).'
        )

        user_content = f"""
REQUESTED MODE: {mode_clean.upper()}
SOURCE TITLE: {source_title or 'N/A'}

<SOURCE_DATA>
{source_text}
</SOURCE_DATA>

Generate the podcast script JSON response adhering to spoken prose rules now.
"""

        url = f"{self._base_url}/api/chat"
        attempt = 1
        t0 = datetime.now(UTC)
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": f"{system_prompt}{json_instruction}"},
                {"role": "user", "content": user_content},
            ],
            "format": "json",
            "stream": False,
        }

        try:
            from herald.concurrency import get_semaphores
            with get_semaphores().script, httpx.Client(timeout=settings.effective_ai_timeout_seconds) as client:
                resp = client.post(url, json=payload)
        except httpx.TimeoutException as timeout_err:
            record_ai_interaction(
                job_id=job_id,
                provider="ollama",
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
                f"Ollama client timeout connecting to {self._model}",
                provider="ollama",
                model=self._model,
                operation="script_generation",
            )
        except Exception as net_err:
            record_ai_interaction(
                job_id=job_id,
                provider="ollama",
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
                f"Ollama network failure: {safe_net_err}",
                provider="ollama",
                model=self._model,
                operation="script_generation",
            )

        if resp.status_code != 200:
            record_ai_interaction(
                job_id=job_id,
                provider="ollama",
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
            if resp.status_code == 404:
                raise AIModelUnavailableError(
                    f"Ollama model '{self._model}' not found: HTTP 404",
                    provider="ollama",
                    model=self._model,
                    http_status=404,
                )
            if resp.status_code >= 500:
                raise AIProviderUnavailableError(
                    f"Ollama API returned HTTP {resp.status_code}",
                    provider="ollama",
                    model=self._model,
                    http_status=resp.status_code,
                )
            raise AIProviderError(
                f"Ollama API error ({resp.status_code}): {resp.text[:300]}",
                provider="ollama",
                model=self._model,
                http_status=resp.status_code,
            )

        result_json = resp.json()
        msg = result_json.get("message", {})
        raw_content = msg.get("content", "")

        p_tok = result_json.get("prompt_eval_count")
        c_tok = result_json.get("eval_count")
        t_tok = (p_tok + c_tok) if (p_tok is not None and c_tok is not None) else None

        try:
            script_dict = _extract_json_block(raw_content)
            parsed_script = PodcastScriptResponse(**script_dict)

            record_ai_interaction(
                job_id=job_id,
                provider="ollama",
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
                provider="ollama",
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
                f"Ollama response schema invalid: {parse_err}",
                provider="ollama",
                model=self._model,
            )

    def distill_text(
        self,
        chunk: str,
        *,
        chunk_index: int = 0,
        total_chunks: int = 1,
        job_id: str | None = None,
    ) -> str:
        """Distill key narrative facts from source chunk using Ollama."""
        from herald.ai.adaptation import DISTILLATION_SYSTEM_PROMPT
        url = f"{self._base_url}/api/chat"
        user_prompt = (
            f"Chunk {chunk_index + 1} of {total_chunks}:\n\n"
            f"<SOURCE_CHUNK>\n{chunk}\n</SOURCE_CHUNK>\n\n"
            "Distill this chunk into concise, structured factual points preserving all entities, "
            "metrics, dates, citations, and qualifiers in logical order."
        )
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": DISTILLATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.2},
        }
        try:
            from herald.concurrency import get_semaphores
            with get_semaphores().script, httpx.Client(timeout=settings.effective_ai_timeout_seconds) as client:
                resp = client.post(url, json=payload)
            if resp.status_code == 404:
                raise AIModelUnavailableError(f"Ollama model {self._model} not found", provider="ollama", model=self._model, operation="distillation")
            if resp.status_code >= 500:
                raise AIProviderUnavailableError(f"Ollama server error HTTP {resp.status_code}", provider="ollama", model=self._model, operation="distillation")
            if resp.status_code != 200:
                raise AIProviderError(f"Ollama error HTTP {resp.status_code}: {resp.text[:200]}", provider="ollama", model=self._model, operation="distillation")
            data = resp.json()
            msg = data.get("message", {})
            return msg.get("content", "").strip()
        except httpx.TimeoutException:
            raise AIClientTimeoutError("Ollama timeout during distillation", provider="ollama", model=self._model, operation="distillation")
        except Exception as e:
            if isinstance(e, AIProviderError):
                raise
            if isinstance(e, httpx.NetworkError):
                raise AIProviderUnavailableError(f"Ollama network error during distillation: {e}", provider="ollama", model=self._model, operation="distillation")
            raise AIProviderError(f"Ollama distillation error: {e}", provider="ollama", model=self._model, operation="distillation")
