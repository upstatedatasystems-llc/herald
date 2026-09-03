"""
Cloudflare Workers AI Provider Implementation for Herald.
Routes script generation requests directly through Cloudflare Workers AI API endpoint,
with truthful one-call/one-record telemetry, bounded evidence, and 1 bounded schema repair attempt.
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
        self._model = (
            model
            or getattr(settings, "CLOUDFLARE_AI_MODEL", None)
            or getattr(settings, "CLOUDFLARE_MODEL", None)
            or "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
        )

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
            return {
                "provider": self.provider_name,
                "configured": True,
                "connected": False,
                "model": self.configured_model,
                "error": sanitize_error(str(e)),
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
            req_evidence = {
                "mode": mode_clean,
                "attempt": attempt,
                "source_character_count": len(source_text),
                "structured_output_mode": "prompt_schema",
                "repair_phase": False,
            }
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
            except Exception as net_err:
                record_ai_interaction(
                    job_id=job_id,
                    provider="cloudflare",
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
                    raise RuntimeError(f"Cloudflare Workers AI network failure: {sanitize_error(str(net_err))}")
                time.sleep(backoff)
                backoff *= 2.0
                continue

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
                    error=f"HTTP {resp.status_code}: {resp.text[:300]}",
                    request_json=req_evidence,
                    response_json={"http_status": resp.status_code, "response_character_count": len(resp.text)},
                    metadata={"attempt": attempt, "mode": mode_clean},
                )
                if attempt == max_attempts:
                    raise RuntimeError(f"Cloudflare Workers AI API error ({resp.status_code})")
                time.sleep(backoff)
                backoff *= 2.0
                continue

            result_json = resp.json()
            raw_content = ""
            if "result" in result_json and isinstance(result_json["result"], dict):
                raw_content = result_json["result"].get("response", "")
            elif "response" in result_json:
                raw_content = result_json.get("response", "")
            elif "choices" in result_json and result_json["choices"]:
                raw_content = result_json["choices"][0].get("message", {}).get("content", "")

            resp_evidence = {
                "http_status": resp.status_code,
                "response_character_count": len(raw_content),
            }

            try:
                script_dict = _extract_json_block(raw_content)
                parsed_script = PodcastScriptResponse(**script_dict)
                resp_evidence["schema_validation"] = "valid"

                # Invariant: Record terminal success ONLY after Pydantic validation passes
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
                    request_json=req_evidence,
                    response_json=resp_evidence,
                    metadata={"attempt": attempt, "mode": mode_clean},
                )
                return parsed_script

            except Exception as parse_err:
                logger.warning(
                    "Cloudflare Workers AI output parsing failed on attempt %d: %s. Initiating 1 bounded repair retry.",
                    attempt,
                    parse_err,
                )
                resp_evidence["schema_validation"] = "failed"
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
                    "structured_output_mode": "prompt_schema",
                    "repair_phase": True,
                }
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

                try:
                    from herald.concurrency import get_semaphores
                    with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                        repair_resp = client.post(url, json=repair_payload, headers=headers)
                except Exception as rep_net_err:
                    record_ai_interaction(
                        job_id=job_id,
                        provider="cloudflare",
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
                        raise RuntimeError(f"Cloudflare Workers AI repair network failure: {sanitize_error(str(rep_net_err))}")
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue

                repair_req_id = repair_resp.headers.get("cf-ray") or repair_resp.headers.get("x-request-id")
                if repair_resp.status_code != 200:
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
                        success=False,
                        error=f"Repair HTTP {repair_resp.status_code}: {repair_resp.text[:300]}",
                        request_json=rep_evidence,
                        response_json={"http_status": repair_resp.status_code, "response_character_count": len(repair_resp.text)},
                        metadata={"attempt": attempt, "mode": mode_clean, "phase": "repair_http_failure"},
                    )
                    if attempt == max_attempts:
                        raise RuntimeError(f"Cloudflare Workers AI repair returned HTTP {repair_resp.status_code}")
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue

                rep_json = repair_resp.json()
                rep_raw = ""
                if "result" in rep_json and isinstance(rep_json["result"], dict):
                    rep_raw = rep_json["result"].get("response", "")
                elif "response" in rep_json:
                    rep_raw = rep_json.get("response", "")

                rep_resp_evidence = {
                    "http_status": repair_resp.status_code,
                    "response_character_count": len(rep_raw),
                }

                try:
                    rep_dict = _extract_json_block(rep_raw)
                    repaired_script = PodcastScriptResponse(**rep_dict)
                    rep_resp_evidence["schema_validation"] = "repaired"

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
                        request_json=rep_evidence,
                        response_json=rep_resp_evidence,
                        metadata={"attempt": attempt, "mode": mode_clean, "phase": "repair_success"},
                    )
                    return repaired_script

                except Exception as rep_parse_err:
                    rep_resp_evidence["schema_validation"] = "failed"
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
                        success=False,
                        error=rep_parse_err,
                        request_json=rep_evidence,
                        response_json=rep_resp_evidence,
                        metadata={"attempt": attempt, "mode": mode_clean, "phase": "repair_parse_failure"},
                    )
                    if attempt == max_attempts:
                        raise RuntimeError(f"Cloudflare Workers AI schema validation and repair failed: {rep_parse_err}")
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue

        raise RuntimeError("Failed to generate podcast script from Cloudflare Workers AI after retries.")
