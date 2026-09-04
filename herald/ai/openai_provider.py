"""
OpenAI-Compatible Provider Implementation for Herald.
Generates structured podcast scripts using Chat Completions API with JSON mode,
truthful one-call/one-record interaction tracking, and 1 bounded schema repair attempt.
"""

import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from herald.ai.base import AIProvider, ProviderCapabilities, load_system_prompt
from herald.ai.schema import PodcastScriptResponse
from herald.config import settings
from herald.services.ai_recorder import record_ai_interaction
from herald.services.redaction import sanitize_error

logger = logging.getLogger("herald.ai.openai")


def _extract_json_block(text: str) -> dict[str, Any]:
    """Extract JSON object from markdown or raw text."""
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
        custom_headers: dict[str, str] | None = None,
    ):
        self._api_key = api_key or settings.OPENAI_API_KEY
        self._model = model or settings.OPENAI_MODEL or "gpt-4o"
        self._api_base = (api_base or settings.OPENAI_API_BASE or "https://api.openai.com/v1").rstrip("/")
        self._custom_provider_name = provider_name
        self._custom_headers = custom_headers or {}

    @property
    def provider_name(self) -> str:
        return self._custom_provider_name

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
        return bool(self._api_key and self._api_key.strip())

    def check_connection(self, timeout_seconds: float = 5.0, force_refresh: bool = False) -> dict[str, Any]:
        if not self.is_configured():
            return {
                "provider": self.provider_name,
                "configured": False,
                "connected": False,
                "model": self.configured_model,
                "error": f"{self.provider_name} credentials are not configured.",
            }

        url = f"{self._api_base}/models"
        headers = {"Authorization": f"Bearer {self._api_key}", **self._custom_headers}

        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                resp_json = resp.json() if resp.text else {}
                data_list = resp_json.get("data", []) if isinstance(resp_json, dict) else []
                model_ids = set()
                for m in data_list:
                    if isinstance(m, dict) and "id" in m:
                        model_ids.add(m["id"])
                    elif isinstance(m, str):
                        model_ids.add(m)

                target_model = self.configured_model.strip()
                if data_list is not None and target_model not in model_ids:
                    return {
                        "provider": self.provider_name,
                        "configured": True,
                        "connected": False,
                        "model": self.configured_model,
                        "error": "configured model unavailable",
                    }
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
            **self._custom_headers,
        }

        max_attempts = settings.GEMINI_RETRY_COUNT
        backoff = 2.0

        for attempt in range(1, max_attempts + 1):
            t0 = datetime.now(UTC)
            req_evidence = {
                "mode": mode_clean,
                "attempt": attempt,
                "source_character_count": len(source_text),
                "structured_output_mode": "json_object",
                "repair_phase": False,
            }
            payload = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": f"{system_prompt}{json_instruction}"},
                    {"role": "user", "content": user_content},
                ],
                "response_format": {"type": "json_object"},
                "temperature": settings.GEMINI_TEMPERATURE,
            }

            try:
                from herald.concurrency import get_semaphores
                with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                    resp = client.post(url, json=payload, headers=headers)
            except Exception as net_err:
                record_ai_interaction(
                    job_id=job_id,
                    provider=self.provider_name.lower(),
                    model=self._model,
                    operation="script_generation",
                    attempt=attempt,
                    started_at=t0,
                    completed_at=datetime.now(UTC),
                    success=False,
                    error=net_err,
                    request_json=req_evidence,
                    metadata={"attempt": attempt, "mode": mode_clean},
                )
                if attempt == max_attempts:
                    _, safe_net_err = sanitize_error(net_err)
                    raise RuntimeError(f"{self.provider_name} network failure: {safe_net_err}")
                time.sleep(backoff)
                backoff *= 2.0
                continue

            req_id = resp.headers.get("x-request-id") or resp.headers.get("cf-ray") or resp.headers.get("openai-organization")
            if resp.status_code != 200:
                record_ai_interaction(
                    job_id=job_id,
                    provider=self.provider_name.lower(),
                    model=self._model,
                    operation="script_generation",
                    attempt=attempt,
                    http_status=resp.status_code,
                    provider_request_id=req_id,
                    input_chars=len(user_content),
                    started_at=t0,
                    completed_at=datetime.now(UTC),
                    success=False,
                    error=f"HTTP {resp.status_code}: {resp.text[:300]}",
                    request_json=req_evidence,
                    response_json={"http_status": resp.status_code, "response_character_count": len(resp.text)},
                    metadata={"attempt": attempt, "mode": mode_clean},
                )
                if attempt == max_attempts:
                    raise RuntimeError(f"{self.provider_name} API returned HTTP {resp.status_code}")
                time.sleep(backoff)
                backoff *= 2.0
                continue

            result_json = resp.json()
            req_id = req_id or result_json.get("id")
            choices = result_json.get("choices", [])
            raw_content = choices[0].get("message", {}).get("content", "") if choices else ""
            usage = result_json.get("usage", {})
            p_tok = usage.get("prompt_tokens")
            c_tok = usage.get("completion_tokens")
            t_tok = usage.get("total_tokens")

            resp_evidence = {
                "http_status": resp.status_code,
                "response_character_count": len(raw_content),
                "finish_reason": choices[0].get("finish_reason") if choices else None,
            }

            # Parse and validate with 1 bounded schema repair attempt if needed
            try:
                script_dict = _extract_json_block(raw_content)
                parsed_script = PodcastScriptResponse(**script_dict)
                resp_evidence["schema_validation"] = "valid"

                # Invariant: Record terminal success only after schema validation succeeds
                record_ai_interaction(
                    job_id=job_id,
                    provider=self.provider_name.lower(),
                    model=self._model,
                    operation="script_generation",
                    attempt=attempt,
                    http_status=resp.status_code,
                    provider_request_id=req_id,
                    input_chars=len(user_content),
                    started_at=t0,
                    completed_at=datetime.now(UTC),
                    success=True,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    request_json=req_evidence,
                    response_json=resp_evidence,
                    metadata={"attempt": attempt, "mode": mode_clean},
                )
                return parsed_script

            except Exception as parse_err:
                logger.warning(
                    "%s output parsing failed on attempt %d: %s. Initiating 1 bounded repair retry.",
                    self.provider_name,
                    attempt,
                    parse_err,
                )
                resp_evidence["schema_validation"] = "failed"
                record_ai_interaction(
                    job_id=job_id,
                    provider=self.provider_name.lower(),
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
                    request_json=req_evidence,
                    response_json=resp_evidence,
                    metadata={"attempt": attempt, "mode": mode_clean, "phase": "parse_failure"},
                )

                # Bounded Repair Attempt (Second HTTP Call)
                t_repair = datetime.now(UTC)
                rep_evidence = {
                    "mode": mode_clean,
                    "attempt": attempt,
                    "source_character_count": len(source_text),
                    "structured_output_mode": "json_object",
                    "repair_phase": True,
                }
                repair_messages = [
                    {"role": "system", "content": f"{system_prompt}{json_instruction}"},
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": raw_content},
                    {
                        "role": "user",
                        "content": (
                            f"Your previous response produced a validation error: {parse_err}. "
                            "Please fix the error and return only the valid JSON response adhering strictly to the schema."
                        ),
                    },
                ]
                repair_payload = {
                    "model": self._model,
                    "messages": repair_messages,
                    "response_format": {"type": "json_object"},
                    "temperature": settings.GEMINI_TEMPERATURE,
                }

                try:
                    from herald.concurrency import get_semaphores
                    with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                        repair_resp = client.post(url, json=repair_payload, headers=headers)
                except Exception as rep_net_err:
                    record_ai_interaction(
                        job_id=job_id,
                        provider=self.provider_name.lower(),
                        model=self._model,
                        operation="script_repair",
                        attempt=attempt,
                        started_at=t_repair,
                        completed_at=datetime.now(UTC),
                        success=False,
                        error=rep_net_err,
                        request_json=rep_evidence,
                        metadata={"attempt": attempt, "mode": mode_clean, "phase": "repair_network_failure"},
                    )
                    if attempt == max_attempts:
                        _, safe_rep_err = sanitize_error(rep_net_err)
                        raise RuntimeError(f"{self.provider_name} repair network failure: {safe_rep_err}")
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue

                repair_req_id = repair_resp.headers.get("x-request-id") or repair_resp.headers.get("cf-ray")
                if repair_resp.status_code != 200:
                    record_ai_interaction(
                        job_id=job_id,
                        provider=self.provider_name.lower(),
                        model=self._model,
                        operation="script_repair",
                        attempt=attempt,
                        http_status=repair_resp.status_code,
                        provider_request_id=repair_req_id,
                        input_chars=len(user_content),
                        started_at=t_repair,
                        completed_at=datetime.now(UTC),
                        success=False,
                        error=f"Repair HTTP {repair_resp.status_code}: {repair_resp.text[:300]}",
                        request_json=rep_evidence,
                        response_json={"http_status": repair_resp.status_code, "response_character_count": len(repair_resp.text)},
                        metadata={"attempt": attempt, "mode": mode_clean, "phase": "repair_http_failure"},
                    )
                    if attempt == max_attempts:
                        raise RuntimeError(f"{self.provider_name} repair returned HTTP {repair_resp.status_code}")
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue

                repair_result_json = repair_resp.json()
                repair_req_id = repair_req_id or repair_result_json.get("id")
                repair_choices = repair_result_json.get("choices", [])
                repair_raw = repair_choices[0].get("message", {}).get("content", "") if repair_choices else ""
                rep_usage = repair_result_json.get("usage", {})
                rep_resp_evidence = {
                    "http_status": repair_resp.status_code,
                    "response_character_count": len(repair_raw),
                    "finish_reason": repair_choices[0].get("finish_reason") if repair_choices else None,
                }

                try:
                    repair_dict = _extract_json_block(repair_raw)
                    repaired_script = PodcastScriptResponse(**repair_dict)
                    rep_resp_evidence["schema_validation"] = "repaired"

                    record_ai_interaction(
                        job_id=job_id,
                        provider=self.provider_name.lower(),
                        model=self._model,
                        operation="script_repair",
                        attempt=attempt,
                        http_status=repair_resp.status_code,
                        provider_request_id=repair_req_id,
                        input_chars=len(user_content),
                        started_at=t_repair,
                        completed_at=datetime.now(UTC),
                        success=True,
                        prompt_tokens=rep_usage.get("prompt_tokens"),
                        completion_tokens=rep_usage.get("completion_tokens"),
                        total_tokens=rep_usage.get("total_tokens"),
                        request_json=rep_evidence,
                        response_json=rep_resp_evidence,
                        metadata={"attempt": attempt, "mode": mode_clean, "phase": "repair_success"},
                    )
                    return repaired_script

                except Exception as rep_parse_err:
                    rep_resp_evidence["schema_validation"] = "failed"
                    record_ai_interaction(
                        job_id=job_id,
                        provider=self.provider_name.lower(),
                        model=self._model,
                        operation="script_repair",
                        attempt=attempt,
                        http_status=repair_resp.status_code,
                        provider_request_id=repair_req_id,
                        input_chars=len(user_content),
                        started_at=t_repair,
                        completed_at=datetime.now(UTC),
                        success=False,
                        error=rep_parse_err,
                        request_json=rep_evidence,
                        response_json=rep_resp_evidence,
                        metadata={"attempt": attempt, "mode": mode_clean, "phase": "repair_parse_failure"},
                    )
                    if attempt == max_attempts:
                        raise RuntimeError(f"{self.provider_name} schema validation and repair failed: {rep_parse_err}")
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue

        raise RuntimeError(f"Failed to generate podcast script from {self.provider_name} after retries.")
