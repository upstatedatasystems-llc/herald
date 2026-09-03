"""
Cloudflare Workers AI Provider Implementation for Herald.
Routes script generation requests directly through Cloudflare Workers AI API endpoint.
"""

import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from herald.ai.base import AIProvider, ProviderCapabilities
from herald.ai.schema import PodcastScriptResponse
from herald.config import settings
from herald.gemini.client import load_system_prompt
from herald.services.ai_recorder import record_ai_interaction

logger = logging.getLogger("herald.ai.cloudflare")


def _extract_json_block(text: str) -> dict[str, Any]:
    """Extract JSON object from text or markdown codeblock."""
    clean = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return json.loads(clean)


class CloudflareProvider(AIProvider):
    def __init__(
        self,
        api_token: str | None = None,
        account_id: str | None = None,
        model: str | None = None,
    ):
        self._api_token = api_token or settings.CLOUDFLARE_API_TOKEN
        self._account_id = account_id or settings.CLOUDFLARE_ACCOUNT_ID
        self._model = model or settings.CLOUDFLARE_MODEL or "@cf/meta/llama-3.3-70b-instruct-fp8-fast"

    @property
    def provider_name(self) -> str:
        return "Cloudflare Workers AI"

    @property
    def configured_model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            script_brief=True,
            script_standard=True,
            structured_output=True,
            research_grounding=False,
            usage_metrics=True,
        )

    def is_configured(self) -> bool:
        return bool(
            self._api_token
            and self._api_token.strip()
            and self._account_id
            and self._account_id.strip()
        )

    def check_connection(self, timeout_seconds: float = 5.0, force_refresh: bool = False) -> dict[str, Any]:
        if not self.is_configured():
            return {
                "provider": self.provider_name,
                "configured": False,
                "connected": False,
                "model": self.configured_model,
                "error": "Cloudflare API token or Account ID is not configured.",
            }

        url = f"https://api.cloudflare.com/client/v4/accounts/{self._account_id.strip()}/ai/models/search"
        headers = {"Authorization": f"Bearer {self._api_token.strip()}"}

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
                "error": f"Cloudflare HTTP {resp.status_code}: {resp.text}",
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
            raise RuntimeError("Cloudflare Workers AI API token or Account ID is not configured.")

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

        model_clean = self._model.strip().lstrip("/")
        url = f"https://api.cloudflare.com/client/v4/accounts/{self._account_id.strip()}/ai/run/{model_clean}"
        headers = {
            "Authorization": f"Bearer {self._api_token.strip()}",
            "Content-Type": "application/json",
        }

        max_attempts = settings.GEMINI_RETRY_COUNT
        backoff = 2.0

        for attempt in range(1, max_attempts + 1):
            t0 = datetime.now(UTC)
            payload = {
                "messages": [
                    {"role": "system", "content": f"{system_prompt}{json_instruction}"},
                    {"role": "user", "content": user_content},
                ],
            }

            try:
                from herald.concurrency import get_semaphores
                with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                    resp = client.post(url, json=payload, headers=headers)

                req_id = resp.headers.get("cf-ray") or resp.headers.get("x-request-id")

                if resp.status_code != 200:
                    record_ai_interaction(
                        job_id=job_id,
                        provider="cloudflare",
                        model=self._model,
                        operation="script_generation",
                        attempt=attempt,
                        http_status=resp.status_code,
                        provider_request_id=req_id,
                        input_chars=len(user_content),
                        started_at=t0,
                        completed_at=datetime.now(UTC),
                        success=False,
                        error=f"HTTP {resp.status_code}: {resp.text}",
                        request_json=payload,
                        metadata={"attempt": attempt, "mode": mode_clean},
                    )
                    if attempt < max_attempts:
                        time.sleep(backoff)
                        backoff *= 2.0
                        continue
                    raise RuntimeError(f"Cloudflare Workers AI API error ({resp.status_code}): {resp.text}")

                result_json = resp.json()
                raw_content = ""
                if "result" in result_json and isinstance(result_json["result"], dict):
                    raw_content = result_json["result"].get("response", "")
                elif "response" in result_json:
                    raw_content = result_json.get("response", "")
                elif "choices" in result_json and result_json["choices"]:
                    raw_content = result_json["choices"][0].get("message", {}).get("content", "")

                try:
                    script_dict = _extract_json_block(raw_content)
                    parsed_script = PodcastScriptResponse(**script_dict)

                    record_ai_interaction(
                        job_id=job_id,
                        provider="cloudflare",
                        model=self._model,
                        operation="script_generation",
                        attempt=attempt,
                        http_status=resp.status_code,
                        provider_request_id=req_id,
                        input_chars=len(user_content),
                        started_at=t0,
                        completed_at=datetime.now(UTC),
                        success=True,
                        request_json=payload,
                        response_json=result_json,
                        metadata={"attempt": attempt, "mode": mode_clean},
                    )
                    return parsed_script

                except Exception as parse_err:
                    logger.warning(
                        "Cloudflare Workers AI output parsing failed on attempt %d: %s. Initiating 1 bounded repair retry.",
                        attempt,
                        parse_err,
                    )
                    record_ai_interaction(
                        job_id=job_id,
                        provider="cloudflare",
                        model=self._model,
                        operation="script_generation",
                        attempt=attempt,
                        http_status=resp.status_code,
                        provider_request_id=req_id,
                        input_chars=len(user_content),
                        started_at=t0,
                        completed_at=datetime.now(UTC),
                        success=False,
                        error=parse_err,
                        request_json=payload,
                        response_json=result_json,
                        metadata={"attempt": attempt, "mode": mode_clean, "phase": "parse_failure"},
                    )

                    # Bounded Repair Attempt
                    t_repair = datetime.now(UTC)
                    repair_payload = {
                        "messages": [
                            {"role": "system", "content": f"{system_prompt}{json_instruction}"},
                            {"role": "user", "content": user_content},
                            {"role": "assistant", "content": raw_content},
                            {
                                "role": "user",
                                "content": (
                                    f"Your previous response produced a validation error: {parse_err}. "
                                    "Please fix the error and return only valid JSON adhering strictly to the schema."
                                ),
                            },
                        ],
                    }

                    with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                        repair_resp = client.post(url, json=repair_payload, headers=headers)

                    repair_req_id = repair_resp.headers.get("cf-ray") or repair_resp.headers.get("x-request-id")
                    if repair_resp.status_code == 200:
                        rep_json = repair_resp.json()
                        rep_raw = ""
                        if "result" in rep_json and isinstance(rep_json["result"], dict):
                            rep_raw = rep_json["result"].get("response", "")
                        elif "response" in rep_json:
                            rep_raw = rep_json.get("response", "")

                        rep_dict = _extract_json_block(rep_raw)
                        repaired_script = PodcastScriptResponse(**rep_dict)

                        record_ai_interaction(
                            job_id=job_id,
                            provider="cloudflare",
                            model=self._model,
                            operation="script_repair",
                            attempt=attempt,
                            http_status=repair_resp.status_code,
                            provider_request_id=repair_req_id,
                            input_chars=len(user_content),
                            started_at=t_repair,
                            completed_at=datetime.now(UTC),
                            success=True,
                            request_json=repair_payload,
                            response_json=rep_json,
                            metadata={"attempt": attempt, "mode": mode_clean, "phase": "repair_success"},
                        )
                        return repaired_script

                    raise parse_err

            except Exception as e:
                record_ai_interaction(
                    job_id=job_id,
                    provider="cloudflare",
                    model=self._model,
                    operation="script_generation",
                    attempt=attempt,
                    started_at=t0,
                    completed_at=datetime.now(UTC),
                    success=False,
                    error=e,
                    request_json=payload,
                    metadata={"attempt": attempt, "mode": mode_clean},
                )
                if attempt == max_attempts:
                    raise RuntimeError(f"Cloudflare Workers AI script generation failed: {e}")
                time.sleep(backoff)
                backoff *= 2.0

        raise RuntimeError("Failed to generate podcast script from Cloudflare Workers AI after retries.")
