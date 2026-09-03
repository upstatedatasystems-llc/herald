"""
OpenAI Provider Implementation for Herald.
Generates structured podcast scripts using OpenAI Chat Completions API with JSON mode.
"""

import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from herald.ai.base import AIProvider
from herald.config import settings
from herald.gemini.client import load_system_prompt
from herald.gemini.schema import PodcastScriptResponse
from herald.services.ai_recorder import record_ai_interaction

logger = logging.getLogger("herald.ai.openai")


def _extract_json_block(text: str) -> dict[str, Any]:
    """Extract JSON object from text."""
    clean = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return json.loads(clean)


class OpenAIProvider(AIProvider):
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        api_base: str | None = None,
        provider_name: str = "OpenAI",
    ):
        self._api_key = api_key or settings.OPENAI_API_KEY
        self._model = model or settings.OPENAI_MODEL or "gpt-4o"
        self._api_base = (api_base or settings.OPENAI_API_BASE or "https://api.openai.com/v1").rstrip("/")
        self._custom_provider_name = provider_name

    @property
    def provider_name(self) -> str:
        return self._custom_provider_name

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
                "error": f"{self.provider_name} API key is not configured.",
            }

        url = f"{self._api_base}/models"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                return {
                    "provider": self.provider_name,
                    "configured": True,
                    "connected": True,
                    "model": self.configured_model,
                    "error": None,
                }
            return {
                "provider": self.provider_name,
                "configured": True,
                "connected": False,
                "model": self.configured_model,
                "error": f"{self.provider_name} HTTP {resp.status_code}: {resp.text}",
            }
        except Exception as e:
            return {
                "provider": self.provider_name,
                "configured": True,
                "connected": False,
                "model": self.configured_model,
                "error": str(e),
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
            raise RuntimeError(f"{self.provider_name} API key is not configured.")

        mode_clean = (request_mode or "standard").lower()
        system_prompt = load_system_prompt()
        json_instruction = (
            "\n\nOutput Schema: Return a JSON object with keys: "
            '"episode_title" (str), "episode_description" (str), "estimated_minutes" (int), '
            '"source_title" (str), "segments" (array of {"order": int, "heading": str, "narration": str}), "warnings" (array of str).'
        )

        user_content = f"""
REQUESTED MODE: {mode_clean.upper()}
SOURCE TITLE: {source_title or 'N/A'}

<SOURCE_DATA>
{source_text}
</SOURCE_DATA>

Generate the podcast script JSON response now.
"""

        url = f"{self._api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        max_attempts = settings.GEMINI_RETRY_COUNT
        backoff = 2.0

        for attempt in range(1, max_attempts + 1):
            t0 = datetime.now(UTC)
            try:
                payload = {
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": f"{system_prompt}{json_instruction}"},
                        {"role": "user", "content": user_content},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": settings.GEMINI_TEMPERATURE,
                }

                from herald.concurrency import get_semaphores
                with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                    resp = client.post(url, json=payload, headers=headers)

                if resp.status_code != 200:
                    record_ai_interaction(
                        job_id=job_id,
                        provider=self.provider_name.lower(),
                        model=self._model,
                        operation="script_generation",
                        started_at=t0,
                        completed_at=datetime.now(UTC),
                        success=False,
                        error=f"HTTP {resp.status_code}: {resp.text}",
                        metadata={"attempt": attempt, "mode": mode_clean},
                    )
                    if attempt < max_attempts:
                        time.sleep(backoff)
                        backoff *= 2.0
                        continue
                    raise RuntimeError(f"{self.provider_name} API error ({resp.status_code}): {resp.text}")

                result_json = resp.json()
                choices = result_json.get("choices", [])
                if not choices:
                    raise ValueError(f"{self.provider_name} returned no response choices.")

                raw_content = choices[0].get("message", {}).get("content", "")
                script_dict = _extract_json_block(raw_content)

                usage = result_json.get("usage", {})
                p_tok = usage.get("prompt_tokens")
                c_tok = usage.get("completion_tokens")
                t_tok = usage.get("total_tokens")

                record_ai_interaction(
                    job_id=job_id,
                    provider=self.provider_name.lower(),
                    model=self._model,
                    operation="script_generation",
                    started_at=t0,
                    completed_at=datetime.now(UTC),
                    success=True,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    metadata={"attempt": attempt, "mode": mode_clean},
                )

                return PodcastScriptResponse(**script_dict)

            except Exception as e:
                record_ai_interaction(
                    job_id=job_id,
                    provider=self.provider_name.lower(),
                    model=self._model,
                    operation="script_generation",
                    started_at=t0,
                    completed_at=datetime.now(UTC),
                    success=False,
                    error=e,
                    metadata={"attempt": attempt, "mode": mode_clean},
                )
                if attempt == max_attempts:
                    raise RuntimeError(f"{self.provider_name} script generation failed: {e}")
                time.sleep(backoff)
                backoff *= 2.0

        raise RuntimeError(f"Failed to generate podcast script from {self.provider_name} after retries.")
