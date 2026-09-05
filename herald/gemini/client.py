import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pydantic

from herald.config import settings
from herald.gemini.schema import (
    FidelityAuditResponse,
    PodcastScriptResponse,
    ResearchAuditResponse,
    ResearchDossierResponse,
)
from herald.services.ai_recorder import record_ai_interaction

logger = logging.getLogger("herald.gemini")


def load_system_prompt() -> str:
    prompt_file = Path(__file__).parent.parent.parent / "prompts" / "podcast_script" / "prompt.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return "Transform the provided source content into a podcast script JSON matching schema."


def _extract_tokens(result_json: dict | None) -> tuple[int | None, int | None, int | None, int | None]:
    """Safely extract prompt, completion, total, and thought tokens from Gemini usageMetadata."""
    if not isinstance(result_json, dict):
        return None, None, None, None
    usage = result_json.get("usageMetadata") or result_json.get("usage_metadata") or {}
    p = usage.get("promptTokenCount") or usage.get("prompt_token_count")
    c = usage.get("candidatesTokenCount") or usage.get("candidates_token_count")
    t = usage.get("totalTokenCount") or usage.get("total_token_count")
    th = (
        usage.get("thoughtsTokenCount")
        or usage.get("thoughtTokenCount")
        or usage.get("thoughts_token_count")
        or usage.get("thought_token_count")
        or usage.get("reasoningTokenCount")
    )
    if th is None:
        cand_details = usage.get("candidatesTokensDetails") or usage.get("candidates_tokens_details") or []
        if isinstance(cand_details, list):
            for d in cand_details:
                if isinstance(d, dict) and str(d.get("modality", "")).upper() in ("THOUGHT", "THOUGHTS"):
                    th = d.get("tokenCount") or d.get("token_count")
                    break
    return p, c, t, th


def _extract_request_id(resp: Any) -> str | None:
    """Safely extract provider request id from response headers."""
    headers = getattr(resp, "headers", None)
    if headers and hasattr(headers, "get"):
        return headers.get("x-goog-request-id") or headers.get("x-request-id")
    return None


class GeminiError(Exception):
    """Base exception for Gemini API integration failures."""


class GeminiAuthError(GeminiError):
    """API key or authentication failure."""


class GeminiQuotaError(GeminiError):
    """Rate limit or quota exceeded failure."""


class GeminiValidationError(GeminiError):
    """Returned response failed schema validation."""


class GeminiOutputTruncatedError(GeminiError):
    """Model output was cut off by the configured token limit (finishReason=MAX_TOKENS)."""


def build_script_thinking_config(
    model_name: str | None,
    request_mode: str | None = "standard",
) -> dict[str, Any] | None:
    """
    Build model-appropriate thinkingConfig for script generation.
    - Brief/Standard + Gemini 3.x -> {"thinkingLevel": "low"}
    - Brief/Standard + Gemini 2.5 -> {"thinkingBudget": 1024}
    - Research mode -> None (preserves model's default reasoning behavior without forcing LOW thinking)
    - Older/unsupported/unknown models -> None (omitted cleanly)
    """
    mode_clean = (request_mode or "standard").lower().strip()
    if mode_clean == "research":
        return None

    if not model_name:
        return None

    m = model_name.lower().strip()
    if "gemini-3." in m or "gemini-3" in m:
        return {"thinkingLevel": "low"}
    elif "gemini-2.5" in m or "gemini-2.0-flash-thinking" in m:
        return {"thinkingBudget": 1024}
    return None


def get_gemini_max_output_tokens_ceiling(model_name: str | None) -> int | None:
    """
    Determine authoritative maximum output token ceiling based on Gemini model family.
    - Gemini 3.x / Gemini 2.5: 65,536 tokens
    - Gemini 2.0 / Gemini 1.5: 8,192 tokens
    - Gemini 1.0 / Gemini Pro: 4,096 tokens
    - Unknown/unrecognized: None (unknown ceiling)
    """
    if not model_name:
        return None
    m = model_name.lower().strip()
    if "gemini-3" in m or "gemini-2.5" in m:
        return 65536
    elif "gemini-2.0" in m or "gemini-1.5" in m:
        return 8192
    elif "gemini-1.0" in m or m == "gemini-pro":
        return 4096
    return None



def _record_gemini_interaction(
    job_id: str | None,
    model: str,
    operation: str,
    started_at: datetime,
    completed_at: datetime,
    success: bool,
    http_status: int | None = None,
    attempt: int = 1,
    input_chars: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    thought_tokens: int | None = None,
    requested_max_output_tokens: int | None = None,
    finish_reason: str | None = None,
    error: Exception | str | None = None,
    error_category: str | None = None,
    error_message: str | None = None,
    request_evidence: dict[str, Any] | None = None,
    response_evidence: dict[str, Any] | None = None,
    provider_request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Helper to record Gemini AI telemetry strictly complying with Migration 014 schema."""
    if not job_id:
        return

    sanitized_req: dict[str, Any] = {
        "operation": operation,
        "attempt": attempt,
        "model": model,
        "structured_output": True if operation != "grounded_research" else False,
    }
    if input_chars is not None:
        sanitized_req["input_chars"] = input_chars
    if requested_max_output_tokens is not None:
        sanitized_req["requested_max_output_tokens"] = requested_max_output_tokens
    if metadata:
        for k in ("mode", "research_depth", "structured_output", "has_material_issues"):
            if k in metadata:
                sanitized_req[k] = metadata[k]
    if request_evidence:
        sanitized_req.update(request_evidence)

    sanitized_resp: dict[str, Any] | None = None
    if http_status is not None or response_evidence is not None or error is not None:
        sanitized_resp = {}
        if http_status is not None:
            sanitized_resp["http_status"] = http_status
        if provider_request_id is not None:
            sanitized_resp["provider_request_id"] = provider_request_id
        if prompt_tokens is not None:
            sanitized_resp["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            sanitized_resp["completion_tokens"] = completion_tokens
        if total_tokens is not None:
            sanitized_resp["total_tokens"] = total_tokens
        if thought_tokens is not None:
            sanitized_resp["thought_tokens"] = thought_tokens
        if finish_reason is not None:
            sanitized_resp["finish_reason"] = finish_reason
        sanitized_resp["validation"] = "valid" if success else "invalid"
        if metadata:
            for k in ("finish_reason", "search_count", "grounding_query_count", "source_count", "score", "material_issues_count"):
                if k in metadata and k not in sanitized_resp:
                    sanitized_resp[k] = metadata[k]
        if response_evidence:
            sanitized_resp.update(response_evidence)

    meta = {"attempt": attempt, "operation": operation}
    if finish_reason is not None:
        meta["finish_reason"] = finish_reason
    if thought_tokens is not None:
        meta["thought_tokens"] = thought_tokens
    if requested_max_output_tokens is not None:
        meta["requested_max_output_tokens"] = requested_max_output_tokens
    if request_evidence:
        meta.update(request_evidence)
    if metadata:
        meta.update(metadata)

    record_ai_interaction(
        job_id=job_id,
        provider="gemini",
        model=model,
        operation=operation,
        started_at=started_at,
        completed_at=completed_at,
        attempt=attempt,
        http_status=http_status,
        provider_request_id=provider_request_id,
        input_chars=input_chars,
        success=success,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        error=error,
        error_category=error_category,
        error_message=error_message,
        request_json=sanitized_req,
        response_json=sanitized_resp,
        metadata=meta,
    )


def generate_grounded_research(
    source_text: str,
    research_depth: str = "medium",
    api_key: str | None = None,
    model_name: str | None = None,
    job_id: str | None = None,
) -> dict:
    """
    Stage 1a: Call GEMINI_RESEARCH_MODEL with Google Search grounding to retrieve external evidence.
    Collects raw response, grounding metadata, search query count, and builds canonical source registry.
    Does NOT silently fall back to ungrounded model knowledge.
    """
    key = api_key or settings.GEMINI_API_KEY
    model = model_name or settings.GEMINI_RESEARCH_MODEL

    if not key:
        raise GeminiAuthError("Gemini API key is not configured.")

    depth = (research_depth or "medium").lower()
    rounds = 1 if depth == "low" else (2 if depth == "medium" else 3)
    target_queries = 4 if depth == "low" else (8 if depth == "medium" else 18)

    prompt = f"""
Perform grounded research for the following primary source material.
RESEARCH DEPTH: {depth.upper()} (Target soft search ceiling: ~{target_queries} queries across up to {rounds} round(s)).

Your goal:
1. Verify major factual claims, numbers, statistics, and dates.
2. Search for authoritative primary and original sources (government agencies, standards bodies, academic papers, official technical documentation).
3. Find useful explanatory context, updates, or recent developments.
4. Identify any meaningful contradictions, outdated claims, or uncertainty.

<PRIMARY_SOURCE>
{source_text}
</PRIMARY_SOURCE>

Report your comprehensive grounded findings in detail.
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": key}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}],
    }

    max_attempts = settings.GEMINI_RETRY_COUNT
    backoff = 2.0

    for attempt in range(1, max_attempts + 1):
        t0 = datetime.now(UTC)
        interaction_recorded = False
        try:
            logger.info(f"Sending grounded research request to Gemini ({model}), attempt {attempt}/{max_attempts}")
            from herald.concurrency import get_semaphores
            with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                resp = client.post(url, json=payload, headers=headers)

            t1 = datetime.now(UTC)
            req_id = _extract_request_id(resp)

            if resp.status_code in (401, 403):
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="grounded_research",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=resp.status_code,
                    attempt=attempt,
                    input_chars=len(prompt),
                    error=f"Gemini API authentication failed ({resp.status_code}): {resp.text}",
                    provider_request_id=req_id,
                    metadata={"research_depth": depth},
                )
                interaction_recorded = True
                raise GeminiAuthError(f"Gemini API authentication failed ({resp.status_code}): {resp.text}")
            elif resp.status_code == 429:
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="grounded_research",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=resp.status_code,
                    attempt=attempt,
                    input_chars=len(prompt),
                    error=f"HTTP 429: {resp.text}",
                    provider_request_id=req_id,
                    metadata={"research_depth": depth},
                )
                interaction_recorded = True
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiQuotaError(f"Gemini API rate limit exceeded: {resp.text}")
            elif resp.status_code != 200:
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="grounded_research",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=resp.status_code,
                    attempt=attempt,
                    input_chars=len(prompt),
                    error=f"HTTP {resp.status_code}: {resp.text}",
                    provider_request_id=req_id,
                    metadata={"research_depth": depth},
                )
                interaction_recorded = True
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiError(f"Gemini API error ({resp.status_code}): {resp.text}")

            result_json = resp.json()
            p_tok, c_tok, t_tok, th_tok = _extract_tokens(result_json)
            candidates = result_json.get("candidates", [])
            if not candidates:
                err_msg = "Gemini API returned no response candidates for grounded research."
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="grounded_research",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=resp.status_code,
                    attempt=attempt,
                    input_chars=len(prompt),
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    thought_tokens=th_tok,
                    error=err_msg,
                    provider_request_id=req_id,
                    metadata={"research_depth": depth},
                )
                interaction_recorded = True
                raise GeminiValidationError(err_msg)

            candidate = candidates[0]
            parts = candidate.get("content", {}).get("parts", [])
            raw_text = "".join(p.get("text", "") for p in parts if "text" in p)

            grounding_meta = candidate.get("groundingMetadata") or candidate.get("grounding_metadata") or {}
            web_queries = grounding_meta.get("webSearchQueries") or grounding_meta.get("search_queries") or []
            grounding_chunks = grounding_meta.get("groundingChunks") or grounding_meta.get("grounding_chunks") or []

            if not grounding_meta and not web_queries and not grounding_chunks:
                logger.warning(f"No grounding metadata returned by Gemini research call ({model}).")

            research_sources = []
            seen_urls = set()
            idx = 1

            for chunk in grounding_chunks:
                web = chunk.get("web", {})
                u = web.get("uri") or web.get("url")
                t = web.get("title") or "Grounded Source"
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    domain = urlparse(u).netloc or "web"
                    source_id = f"S{idx}"
                    q = web_queries[0] if web_queries else "grounded search"
                    research_sources.append(
                        {
                            "source_id": source_id,
                            "title": t,
                            "url": u,
                            "domain": domain,
                            "retrieved_at": datetime.now(UTC).isoformat(),
                            "search_query": q,
                        }
                    )
                    idx += 1

            if not research_sources and web_queries:
                for q in web_queries:
                    source_id = f"S{idx}"
                    research_sources.append(
                        {
                            "source_id": source_id,
                            "title": f"Grounded Search: {q}",
                            "url": f"https://www.google.com/search?q={q}",
                            "domain": "google.com",
                            "retrieved_at": datetime.now(UTC).isoformat(),
                            "search_query": q,
                        }
                    )
                    idx += 1

            _record_gemini_interaction(
                job_id=job_id,
                model=model,
                operation="grounded_research",
                started_at=t0,
                completed_at=t1,
                success=True,
                http_status=resp.status_code,
                attempt=attempt,
                input_chars=len(prompt),
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=t_tok,
                thought_tokens=th_tok,
                provider_request_id=req_id,
                metadata={
                    "research_depth": depth,
                    "search_count": len(web_queries),
                    "source_count": len(research_sources),
                },
            )
            interaction_recorded = True

            return {
                "raw_text": raw_text,
                "grounding_metadata": grounding_meta,
                "search_count": len(web_queries),
                "source_count": len(research_sources),
                "research_sources": research_sources,
            }

        except Exception as e:
            if not interaction_recorded:
                t1 = datetime.now(UTC)
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="grounded_research",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    attempt=attempt,
                    input_chars=len(prompt),
                    error=e,
                    metadata={"research_depth": depth},
                )
                interaction_recorded = True
            if isinstance(e, (GeminiAuthError, GeminiQuotaError, GeminiValidationError)):
                raise
            if attempt == max_attempts:
                raise GeminiError(f"Grounded research failed: {e}")
            time.sleep(backoff)
            backoff *= 2.0

    raise GeminiError("Failed to perform grounded research after retries.")


def normalize_research_dossier(
    source_text: str,
    grounded_research_data: dict,
    api_key: str | None = None,
    model_name: str | None = None,
    job_id: str | None = None,
) -> ResearchDossierResponse:
    """
    Stage 1b: Second non-search structured-output call using GEMINI_MODEL.
    Receives SOURCE_DATA, GROUNDED_RESEARCH_DATA, and canonical research_sources registry.
    Converts evidence into ResearchDossierResponse and rejects any invalid source_ids.
    """
    key = api_key or settings.GEMINI_API_KEY
    model = model_name or settings.GEMINI_MODEL

    if not key:
        raise GeminiAuthError("Gemini API key is not configured.")

    sources_registry = grounded_research_data.get("research_sources", [])
    valid_source_ids = {s["source_id"] for s in sources_registry}

    prompt = f"""
You are a research analyst normalizing grounded evidence into a structured Research Dossier.

<PRIMARY_SOURCE>
{source_text}
</PRIMARY_SOURCE>

<GROUNDED_RESEARCH_EVIDENCE>
{grounded_research_data.get('raw_text', '')}
</GROUNDED_RESEARCH_EVIDENCE>

<CANONICAL_SOURCE_REGISTRY>
{json.dumps(sources_registry, indent=2)}
</CANONICAL_SOURCE_REGISTRY>

Requirements:
1. Summarize primary source in source_summary.
2. In verification and useful_context, populate source_ids ONLY with valid IDs from CANONICAL_SOURCE_REGISTRY ({list(valid_source_ids)}).
3. Do NOT invent new source IDs or URL strings.
4. Pass the exact CANONICAL_SOURCE_REGISTRY back into research_sources field.
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": key}

    schema_dict = {
        "type": "OBJECT",
        "properties": {
            "source_summary": {"type": "STRING"},
            "verification": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "source_claim": {"type": "STRING"},
                        "status": {"type": "STRING"},
                        "notes": {"type": "STRING"},
                        "source_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
                    },
                    "required": ["source_claim", "status", "notes", "source_ids"],
                },
            },
            "useful_context": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "fact": {"type": "STRING"},
                        "why_it_matters": {"type": "STRING"},
                        "source_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
                    },
                    "required": ["fact", "why_it_matters", "source_ids"],
                },
            },
            "outdated_or_uncertain": {"type": "ARRAY", "items": {"type": "STRING"}},
            "research_sources": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "source_id": {"type": "STRING"},
                        "title": {"type": "STRING"},
                        "url": {"type": "STRING"},
                        "domain": {"type": "STRING"},
                        "retrieved_at": {"type": "STRING"},
                        "search_query": {"type": "STRING"},
                    },
                    "required": ["source_id", "title", "url", "domain", "retrieved_at", "search_query"],
                },
            },
        },
        "required": ["source_summary", "verification", "useful_context", "outdated_or_uncertain", "research_sources"],
    }

    max_attempts = settings.GEMINI_RETRY_COUNT
    backoff = 2.0

    for attempt in range(1, max_attempts + 1):
        t0 = datetime.now(UTC)
        interaction_recorded = False
        try:
            logger.info(f"Sending dossier normalization request to Gemini ({model}), attempt {attempt}/{max_attempts}")
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": settings.GEMINI_TEMPERATURE,
                    "maxOutputTokens": settings.GEMINI_MAX_OUTPUT_TOKENS,
                    "responseMimeType": "application/json",
                    "responseSchema": schema_dict,
                },
            }

            from herald.concurrency import get_semaphores
            with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                resp = client.post(url, json=payload, headers=headers)

            t1 = datetime.now(UTC)
            req_id = _extract_request_id(resp)

            if resp.status_code != 200:
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="dossier_normalization",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=resp.status_code,
                    attempt=attempt,
                    input_chars=len(prompt),
                    error=f"HTTP {resp.status_code}: {resp.text}",
                    provider_request_id=req_id,
                )
                interaction_recorded = True
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiError(f"Gemini error ({resp.status_code}): {resp.text}")

            result_json = resp.json()
            p_tok, c_tok, t_tok, th_tok = _extract_tokens(result_json)
            candidates = result_json.get("candidates", [])
            if not candidates:
                err_msg = "Gemini API returned no response candidates for dossier normalization."
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="dossier_normalization",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=resp.status_code,
                    attempt=attempt,
                    input_chars=len(prompt),
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    thought_tokens=th_tok,
                    error=err_msg,
                    provider_request_id=req_id,
                )
                interaction_recorded = True
                raise GeminiValidationError(err_msg)

            raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            data = json.loads(raw_text)

            for v in data.get("verification", []):
                for sid in v.get("source_ids", []):
                    if valid_source_ids and sid not in valid_source_ids:
                        raise GeminiValidationError(f"Dossier referenced invalid source ID '{sid}' not in registry.")

            for c in data.get("useful_context", []):
                for sid in c.get("source_ids", []):
                    if valid_source_ids and sid not in valid_source_ids:
                        raise GeminiValidationError(f"Dossier referenced invalid source ID '{sid}' not in registry.")

            if not data.get("research_sources") and sources_registry:
                data["research_sources"] = sources_registry

            response_obj = ResearchDossierResponse(**data)

            _record_gemini_interaction(
                job_id=job_id,
                model=model,
                operation="dossier_normalization",
                started_at=t0,
                completed_at=t1,
                success=True,
                http_status=resp.status_code,
                attempt=attempt,
                input_chars=len(prompt),
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=t_tok,
                thought_tokens=th_tok,
                provider_request_id=req_id,
            )
            interaction_recorded = True

            return response_obj

        except (json.JSONDecodeError, pydantic.ValidationError, GeminiValidationError) as e:
            if not interaction_recorded:
                t1 = datetime.now(UTC)
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="dossier_normalization",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=getattr(resp, "status_code", None) if "resp" in locals() else None,
                    attempt=attempt,
                    input_chars=len(prompt),
                    prompt_tokens=p_tok if "p_tok" in locals() else None,
                    completion_tokens=c_tok if "c_tok" in locals() else None,
                    total_tokens=t_tok if "t_tok" in locals() else None,
                    error=e,
                    provider_request_id=req_id if "req_id" in locals() else None,
                )
                interaction_recorded = True
            if attempt == max_attempts:
                raise GeminiValidationError(f"Invalid dossier schema returned by Gemini: {e}")
            time.sleep(backoff)
            backoff *= 2.0
        except Exception as e:
            if not interaction_recorded:
                t1 = datetime.now(UTC)
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="dossier_normalization",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    attempt=attempt,
                    input_chars=len(prompt),
                    error=e,
                )
                interaction_recorded = True
            if isinstance(e, (GeminiAuthError, GeminiQuotaError, GeminiValidationError)):
                raise
            if attempt == max_attempts:
                raise GeminiError(f"Dossier normalization failed: {e}")
            time.sleep(backoff)
            backoff *= 2.0

    raise GeminiError("Failed to normalize research dossier after retries.")


def generate_podcast_script(
    source_text: str,
    request_mode: str = "standard",
    research_dossier: dict | None = None,
    source_title: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
    job_id: str | None = None,
) -> PodcastScriptResponse:
    """
    Generate structured podcast script using GEMINI_MODEL (non-search call).
    Brief/Standard use ONLY source_text. Research mode uses source_text + research_dossier.
    """
    key = api_key or settings.GEMINI_API_KEY
    model = model_name or settings.GEMINI_MODEL

    if not key:
        raise GeminiAuthError("Gemini API key is not configured.")

    system_prompt = load_system_prompt()
    mode_clean = (request_mode or "standard").lower()

    if mode_clean == "research" and research_dossier:
        input_context = f"""
<SOURCE_DATA>
{source_text}
</SOURCE_DATA>

<VERIFIED_RESEARCH_DOSSIER>
{json.dumps(research_dossier, indent=2)}
</VERIFIED_RESEARCH_DOSSIER>
"""
    else:
        input_context = f"""
<SOURCE_DATA>
{source_text}
</SOURCE_DATA>
"""

    user_prompt = f"""
REQUESTED MODE: {mode_clean.upper()}
SOURCE TITLE: {source_title or 'N/A'}

{input_context}

Generate the podcast script JSON response adhering to spoken prose rules and output schema now.
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": key}

    schema_dict = {
        "type": "OBJECT",
        "properties": {
            "episode_title": {"type": "STRING"},
            "episode_description": {"type": "STRING"},
            "estimated_minutes": {"type": "INTEGER"},
            "source_title": {"type": "STRING"},
            "segments": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "order": {"type": "INTEGER"},
                        "heading": {"type": "STRING"},
                        "narration": {"type": "STRING"},
                    },
                    "required": ["order", "heading", "narration"],
                },
            },
            "warnings": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
        },
        "required": [
            "episode_title",
            "episode_description",
            "segments",
            "warnings",
        ],
    }

    max_attempts = settings.GEMINI_RETRY_COUNT
    backoff = 2.0
    last_error = ""
    last_finish_reason = ""

    model_ceiling = get_gemini_max_output_tokens_ceiling(model)
    if mode_clean == "research":
        base_max_tokens = getattr(settings, "GEMINI_MAX_OUTPUT_TOKENS", 4096) or 4096
    else:
        base_max_tokens = getattr(settings, "GEMINI_SCRIPT_MAX_OUTPUT_TOKENS", 16384) or 16384

    current_max_tokens = min(base_max_tokens, model_ceiling) if model_ceiling is not None else base_max_tokens

    for attempt in range(1, max_attempts + 1):
        t0 = datetime.now(UTC)
        interaction_recorded = False
        prompt_content = f"{system_prompt}\n\n{user_prompt}"
        if attempt > 1 and last_error and last_finish_reason != "MAX_TOKENS":
            prompt_content += f"\n\nNOTE: Previous attempt failed validation: '{last_error}'. Strictly conform to required fields."

        thinking_cfg = build_script_thinking_config(model, request_mode=mode_clean)
        request_evidence: dict[str, Any] = {}
        if thinking_cfg:
            if "thinkingLevel" in thinking_cfg:
                request_evidence["thinking_level"] = thinking_cfg["thinkingLevel"]
            if "thinkingBudget" in thinking_cfg:
                request_evidence["thinking_budget"] = thinking_cfg["thinkingBudget"]

        try:
            logger.info(f"Sending script request to Gemini ({model}), mode={mode_clean}, max_tokens={current_max_tokens}, attempt {attempt}/{max_attempts}")
            gen_config: dict[str, Any] = {
                "temperature": settings.GEMINI_TEMPERATURE,
                "maxOutputTokens": current_max_tokens,
                "responseMimeType": "application/json",
                "responseSchema": schema_dict,
            }
            if thinking_cfg:
                gen_config["thinkingConfig"] = thinking_cfg

            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt_content}]}],
                "generationConfig": gen_config,
            }

            from herald.concurrency import get_semaphores
            with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                resp = client.post(url, json=payload, headers=headers)

            t1 = datetime.now(UTC)
            req_id = _extract_request_id(resp)

            if resp.status_code in (401, 403):
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="script_generation",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=resp.status_code,
                    attempt=attempt,
                    input_chars=len(prompt_content),
                    requested_max_output_tokens=current_max_tokens,
                    request_evidence=request_evidence or None,
                    error=f"Gemini API authentication failed ({resp.status_code}): {resp.text}",
                    provider_request_id=req_id,
                    metadata={"mode": mode_clean},
                )
                interaction_recorded = True
                raise GeminiAuthError(f"Gemini API authentication failed ({resp.status_code}): {resp.text}")
            elif resp.status_code == 429:
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="script_generation",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=resp.status_code,
                    attempt=attempt,
                    input_chars=len(prompt_content),
                    requested_max_output_tokens=current_max_tokens,
                    request_evidence=request_evidence or None,
                    error=f"HTTP 429: {resp.text}",
                    provider_request_id=req_id,
                    metadata={"mode": mode_clean},
                )
                interaction_recorded = True
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiQuotaError(f"Gemini API rate limit exceeded: {resp.text}")
            elif resp.status_code != 200:
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="script_generation",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=resp.status_code,
                    attempt=attempt,
                    input_chars=len(prompt_content),
                    requested_max_output_tokens=current_max_tokens,
                    request_evidence=request_evidence or None,
                    error=f"HTTP {resp.status_code}: {resp.text}",
                    provider_request_id=req_id,
                    metadata={"mode": mode_clean},
                )
                interaction_recorded = True
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiError(f"Gemini API error ({resp.status_code}): {resp.text}")

            result_json = resp.json()
            p_tok, c_tok, t_tok, th_tok = _extract_tokens(result_json)
            candidates = result_json.get("candidates", [])
            if not candidates:
                err_msg = "Gemini API returned no response candidates."
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="script_generation",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=resp.status_code,
                    attempt=attempt,
                    input_chars=len(prompt_content),
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    thought_tokens=th_tok,
                    requested_max_output_tokens=current_max_tokens,
                    request_evidence=request_evidence or None,
                    error=err_msg,
                    provider_request_id=req_id,
                    metadata={"mode": mode_clean},
                )
                interaction_recorded = True
                raise GeminiValidationError(err_msg)

            candidate = candidates[0]
            finish_reason = candidate.get("finishReason") or candidate.get("finish_reason") or "STOP"
            parts = candidate.get("content", {}).get("parts", [])
            if not parts or "text" not in parts[0]:
                err_msg = "Gemini candidate content missing text part."
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="script_generation",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=resp.status_code,
                    attempt=attempt,
                    input_chars=len(prompt_content),
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    thought_tokens=th_tok,
                    requested_max_output_tokens=current_max_tokens,
                    request_evidence=request_evidence or None,
                    finish_reason=finish_reason,
                    error=err_msg,
                    provider_request_id=req_id,
                    metadata={"mode": mode_clean, "finish_reason": finish_reason},
                )
                interaction_recorded = True
                raise GeminiValidationError(err_msg)

            raw_text = parts[0]["text"]
            script_data = json.loads(raw_text)
            response_obj = PodcastScriptResponse(**script_data)

            _record_gemini_interaction(
                job_id=job_id,
                model=model,
                operation="script_generation",
                started_at=t0,
                completed_at=t1,
                success=True,
                http_status=resp.status_code,
                attempt=attempt,
                input_chars=len(prompt_content),
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=t_tok,
                thought_tokens=th_tok,
                requested_max_output_tokens=current_max_tokens,
                request_evidence=request_evidence or None,
                finish_reason=finish_reason,
                provider_request_id=req_id,
                metadata={"mode": mode_clean, "finish_reason": finish_reason},
            )
            interaction_recorded = True

            return response_obj

        except (json.JSONDecodeError, pydantic.ValidationError, GeminiValidationError) as e:
            last_error = str(e)
            cand_list = locals().get("candidates", [])
            f_reason = (cand_list[0].get("finishReason") or cand_list[0].get("finish_reason") or "STOP") if cand_list else "STOP"
            last_finish_reason = f_reason

            if not interaction_recorded:
                t1 = datetime.now(UTC)
                is_trunc = f_reason == "MAX_TOKENS"
                err_to_record = (
                    GeminiOutputTruncatedError(f"Gemini output truncated by max_output_tokens limit ({current_max_tokens} tokens, finishReason=MAX_TOKENS): {e}")
                    if is_trunc
                    else e
                )
                err_category = "OUTPUT_TRUNCATED" if is_trunc else "SCHEMA_VALIDATION_ERROR"
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="script_generation",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    http_status=getattr(resp, "status_code", None) if "resp" in locals() else None,
                    attempt=attempt,
                    input_chars=len(prompt_content),
                    prompt_tokens=p_tok if "p_tok" in locals() else None,
                    completion_tokens=c_tok if "c_tok" in locals() else None,
                    total_tokens=t_tok if "t_tok" in locals() else None,
                    thought_tokens=th_tok if "th_tok" in locals() else None,
                    requested_max_output_tokens=current_max_tokens,
                    request_evidence=request_evidence or None,
                    finish_reason=f_reason,
                    error=err_to_record,
                    error_category=err_category,
                    error_message=str(err_to_record),
                    provider_request_id=req_id if "req_id" in locals() else None,
                    metadata={"mode": mode_clean, "finish_reason": f_reason, "truncated": is_trunc},
                )
                interaction_recorded = True

            if f_reason == "MAX_TOKENS":
                logger.warning(
                    f"Gemini script generation truncated at {current_max_tokens} tokens on attempt {attempt}/{max_attempts}: {e}"
                )
                if mode_clean != "research" and model_ceiling is not None:
                    next_tokens = min(current_max_tokens * 2, model_ceiling)
                    if next_tokens > current_max_tokens and attempt < max_attempts:
                        logger.info(
                            f"Retrying Gemini script generation with increased output budget: {current_max_tokens} -> {next_tokens} tokens (ceiling: {model_ceiling})"
                        )
                        current_max_tokens = next_tokens
                        time.sleep(backoff)
                        backoff *= 2.0
                        continue
                raise GeminiOutputTruncatedError(
                    f"Gemini output truncated by max_output_tokens limit ({current_max_tokens} tokens, finishReason=MAX_TOKENS): {e}"
                )
            else:
                logger.error(f"Failed to parse or validate Gemini JSON output on attempt {attempt}: {e}")
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiValidationError(f"Invalid JSON/schema returned by Gemini: {e}")

        except Exception as e:
            if not interaction_recorded:
                t1 = datetime.now(UTC)
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="script_generation",
                    started_at=t0,
                    completed_at=t1,
                    success=False,
                    attempt=attempt,
                    input_chars=len(prompt_content),
                    requested_max_output_tokens=current_max_tokens,
                    request_evidence=request_evidence or None,
                    error=e,
                    metadata={"mode": mode_clean},
                )
                interaction_recorded = True
            if isinstance(e, (GeminiAuthError, GeminiQuotaError, GeminiValidationError, GeminiOutputTruncatedError)):
                raise
            last_error = str(e)
            logger.error(f"Gemini client error on attempt {attempt}: {e}")
            if attempt == max_attempts:
                raise GeminiError(f"Gemini script generation failed: {e}")

        time.sleep(backoff)
        backoff *= 2.0

    raise GeminiError("Failed to generate podcast script after retries.")


def audit_research_script(
    source_text: str,
    research_dossier: dict,
    script_dict: dict,
    api_key: str | None = None,
    model_name: str | None = None,
    job_id: str | None = None,
) -> ResearchAuditResponse:
    """
    Stage 3: Post-generation research audit.
    Evaluates script against source + dossier for factual defects, changed units, or omitted facts.
    """
    key = api_key or settings.GEMINI_API_KEY
    model = model_name or settings.GEMINI_MODEL

    if not key:
        raise GeminiAuthError("Gemini API key is not configured.")

    prompt = f"""
Audit the following podcast script against the primary source and verified research dossier.

<PRIMARY_SOURCE>
{source_text}
</PRIMARY_SOURCE>

<RESEARCH_DOSSIER>
{json.dumps(research_dossier, indent=2)}
</RESEARCH_DOSSIER>

<PODCAST_SCRIPT>
{json.dumps(script_dict, indent=2)}
</PODCAST_SCRIPT>

Check specifically for:
- unsupported_claims
- misrepresented_source_claims
- research_claims_without_evidence
- contradictions_not_disclosed
- important_verified_information_omitted
- changed_numbers_or_units
- citation_mapping_failures

Set has_material_issues to true ONLY if material factual errors or severe misrepresentations exist that require script repair.
If has_material_issues is true, provide concrete repair_instructions.
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": key}

    schema_dict = {
        "type": "OBJECT",
        "properties": {
            "unsupported_claims": {"type": "ARRAY", "items": {"type": "STRING"}},
            "misrepresented_source_claims": {"type": "ARRAY", "items": {"type": "STRING"}},
            "research_claims_without_evidence": {"type": "ARRAY", "items": {"type": "STRING"}},
            "contradictions_not_disclosed": {"type": "ARRAY", "items": {"type": "STRING"}},
            "important_verified_information_omitted": {"type": "ARRAY", "items": {"type": "STRING"}},
            "changed_numbers_or_units": {"type": "ARRAY", "items": {"type": "STRING"}},
            "citation_mapping_failures": {"type": "ARRAY", "items": {"type": "STRING"}},
            "has_material_issues": {"type": "BOOLEAN"},
            "repair_instructions": {"type": "STRING"},
        },
        "required": [
            "unsupported_claims",
            "misrepresented_source_claims",
            "research_claims_without_evidence",
            "contradictions_not_disclosed",
            "important_verified_information_omitted",
            "changed_numbers_or_units",
            "citation_mapping_failures",
            "has_material_issues",
        ],
    }

    t0 = datetime.now(UTC)
    interaction_recorded = False
    try:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": settings.GEMINI_MAX_OUTPUT_TOKENS,
                "responseMimeType": "application/json",
                "responseSchema": schema_dict,
            },
        }

        from herald.concurrency import get_semaphores
        with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload, headers=headers)

        t1 = datetime.now(UTC)
        req_id = _extract_request_id(resp)

        if resp.status_code == 200:
            result_json = resp.json()
            candidates = result_json.get("candidates", [])
            if candidates:
                raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                p_tok, c_tok, t_tok, th_tok = _extract_tokens(result_json)
                audit_obj = ResearchAuditResponse(**json.loads(raw_text))
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="research_audit",
                    started_at=t0,
                    completed_at=t1,
                    success=True,
                    http_status=resp.status_code,
                    input_chars=len(prompt),
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    thought_tokens=th_tok,
                    provider_request_id=req_id,
                    metadata={"has_material_issues": bool(audit_obj.has_material_issues)},
                )
                interaction_recorded = True
                return audit_obj

        _record_gemini_interaction(
            job_id=job_id,
            model=model,
            operation="research_audit",
            started_at=t0,
            completed_at=t1,
            success=False,
            http_status=resp.status_code,
            input_chars=len(prompt),
            error=f"HTTP {resp.status_code}: {resp.text}",
            provider_request_id=req_id,
        )
        interaction_recorded = True
    except Exception as e:
        if not interaction_recorded:
            t1 = datetime.now(UTC)
            _record_gemini_interaction(
                job_id=job_id,
                model=model,
                operation="research_audit",
                started_at=t0,
                completed_at=t1,
                success=False,
                input_chars=len(prompt),
                error=e,
            )
            interaction_recorded = True
        logger.warning(f"Research audit error: {e}")

    return ResearchAuditResponse(has_material_issues=False)


def repair_research_script(
    source_text: str,
    research_dossier: dict,
    script_dict: dict,
    audit_result: dict,
    api_key: str | None = None,
    model_name: str | None = None,
    job_id: str | None = None,
) -> PodcastScriptResponse:
    """
    Stage 4: Perform ONE targeted script repair pass using audit findings.
    """
    key = api_key or settings.GEMINI_API_KEY
    model = model_name or settings.GEMINI_MODEL

    if not key:
        raise GeminiAuthError("Gemini API key is not configured.")

    prompt = f"""
Repair the podcast script to resolve the audit findings described below.

<AUDIT_FINDINGS>
{json.dumps(audit_result, indent=2)}
</AUDIT_FINDINGS>

<PRIMARY_SOURCE>
{source_text}
</PRIMARY_SOURCE>

<RESEARCH_DOSSIER>
{json.dumps(research_dossier, indent=2)}
</RESEARCH_DOSSIER>

<ORIGINAL_SCRIPT>
{json.dumps(script_dict, indent=2)}
</ORIGINAL_SCRIPT>

Return the corrected PodcastScriptResponse JSON now.
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": key}

    schema_dict = {
        "type": "OBJECT",
        "properties": {
            "episode_title": {"type": "STRING"},
            "episode_description": {"type": "STRING"},
            "source_title": {"type": "STRING"},
            "segments": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "order": {"type": "INTEGER"},
                        "heading": {"type": "STRING"},
                        "narration": {"type": "STRING"},
                    },
                    "required": ["order", "heading", "narration"],
                },
            },
            "warnings": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["episode_title", "episode_description", "segments", "warnings"],
    }

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": settings.GEMINI_MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
            "responseSchema": schema_dict,
        },
    }

    t0 = datetime.now(UTC)
    interaction_recorded = False
    try:
        from herald.concurrency import get_semaphores
        with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload, headers=headers)

        t1 = datetime.now(UTC)
        req_id = _extract_request_id(resp)

        if resp.status_code == 200:
            result_json = resp.json()
            candidates = result_json.get("candidates", [])
            if candidates:
                raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                p_tok, c_tok, t_tok, th_tok = _extract_tokens(result_json)
                repair_obj = PodcastScriptResponse(**json.loads(raw_text))
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="research_repair",
                    started_at=t0,
                    completed_at=t1,
                    success=True,
                    http_status=resp.status_code,
                    input_chars=len(prompt),
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    thought_tokens=th_tok,
                    provider_request_id=req_id,
                )
                interaction_recorded = True
                return repair_obj

        _record_gemini_interaction(
            job_id=job_id,
            model=model,
            operation="research_repair",
            started_at=t0,
            completed_at=t1,
            success=False,
            http_status=resp.status_code,
            input_chars=len(prompt),
            error=f"HTTP {resp.status_code}: {resp.text}",
            provider_request_id=req_id,
        )
        interaction_recorded = True
    except Exception as e:
        if not interaction_recorded:
            t1 = datetime.now(UTC)
            _record_gemini_interaction(
                job_id=job_id,
                model=model,
                operation="research_repair",
                started_at=t0,
                completed_at=t1,
                success=False,
                input_chars=len(prompt),
                error=e,
            )
            interaction_recorded = True
        raise GeminiError(f"Failed to repair research script: {e}")

    raise GeminiError("Failed to repair research script.")


def audit_script_fidelity(
    source_text: str,
    script_dict: dict,
    api_key: str | None = None,
    model_name: str | None = None,
    job_id: str | None = None,
) -> FidelityAuditResponse:
    """
    Perform a Gemini fidelity audit against normalized source text for Brief/Standard script validation when verify=true.
    """
    key = api_key or settings.GEMINI_API_KEY
    model = model_name or settings.GEMINI_MODEL

    if not key:
        raise GeminiAuthError("Gemini API key is not configured.")

    prompt = f"""
Audit the following podcast script strictly against the primary source material.

<PRIMARY_SOURCE>
{source_text}
</PRIMARY_SOURCE>

<PODCAST_SCRIPT>
{json.dumps(script_dict, indent=2)}
</PODCAST_SCRIPT>

Check specifically for:
1. unsupported_factual_claims
2. incorrect_numbers_dates_names
3. incorrect_entity_relationships
4. material_source_misrepresentation
5. important_omissions_material_meaning
6. excessive_certainty
7. accidental_invented_context

Set has_material_issues to true ONLY if material factual errors or severe misrepresentations exist that require script repair.
If has_material_issues is true, provide concrete repair_instructions.
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": key}

    schema_dict = {
        "type": "OBJECT",
        "properties": {
            "unsupported_factual_claims": {"type": "ARRAY", "items": {"type": "STRING"}},
            "incorrect_numbers_dates_names": {"type": "ARRAY", "items": {"type": "STRING"}},
            "incorrect_entity_relationships": {"type": "ARRAY", "items": {"type": "STRING"}},
            "material_source_misrepresentation": {"type": "ARRAY", "items": {"type": "STRING"}},
            "important_omissions_material_meaning": {"type": "ARRAY", "items": {"type": "STRING"}},
            "excessive_certainty": {"type": "ARRAY", "items": {"type": "STRING"}},
            "accidental_invented_context": {"type": "ARRAY", "items": {"type": "STRING"}},
            "has_material_issues": {"type": "BOOLEAN"},
            "repair_instructions": {"type": "STRING"},
        },
        "required": [
            "unsupported_factual_claims",
            "incorrect_numbers_dates_names",
            "incorrect_entity_relationships",
            "material_source_misrepresentation",
            "important_omissions_material_meaning",
            "excessive_certainty",
            "accidental_invented_context",
            "has_material_issues",
        ],
    }

    t0 = datetime.now(UTC)
    interaction_recorded = False
    try:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": settings.GEMINI_MAX_OUTPUT_TOKENS,
                "responseMimeType": "application/json",
                "responseSchema": schema_dict,
            },
        }

        from herald.concurrency import get_semaphores
        with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload, headers=headers)

        t1 = datetime.now(UTC)
        req_id = _extract_request_id(resp)

        if resp.status_code == 200:
            result_json = resp.json()
            candidates = result_json.get("candidates", [])
            if candidates:
                raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                p_tok, c_tok, t_tok, th_tok = _extract_tokens(result_json)
                audit_obj = FidelityAuditResponse(**json.loads(raw_text))
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="fidelity_audit",
                    started_at=t0,
                    completed_at=t1,
                    success=True,
                    http_status=resp.status_code,
                    input_chars=len(prompt),
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    thought_tokens=th_tok,
                    provider_request_id=req_id,
                    metadata={"has_material_issues": bool(audit_obj.has_material_issues)},
                )
                interaction_recorded = True
                return audit_obj

        _record_gemini_interaction(
            job_id=job_id,
            model=model,
            operation="fidelity_audit",
            started_at=t0,
            completed_at=t1,
            success=False,
            http_status=resp.status_code,
            input_chars=len(prompt),
            error=f"HTTP {resp.status_code}: {resp.text}",
            provider_request_id=req_id,
        )
        interaction_recorded = True
    except Exception as e:
        if not interaction_recorded:
            t1 = datetime.now(UTC)
            _record_gemini_interaction(
                job_id=job_id,
                model=model,
                operation="fidelity_audit",
                started_at=t0,
                completed_at=t1,
                success=False,
                input_chars=len(prompt),
                error=e,
            )
            interaction_recorded = True
        logger.warning(f"Fidelity audit error: {e}")

    return FidelityAuditResponse(has_material_issues=False)


def repair_script_fidelity(
    source_text: str,
    script_dict: dict,
    audit_result: dict,
    api_key: str | None = None,
    model_name: str | None = None,
    job_id: str | None = None,
) -> PodcastScriptResponse:
    """
    Perform ONE controlled script repair pass for Brief/Standard script when verify=true.
    """
    key = api_key or settings.GEMINI_API_KEY
    model = model_name or settings.GEMINI_MODEL

    if not key:
        raise GeminiAuthError("Gemini API key is not configured.")

    prompt = f"""
Repair the podcast script to resolve the fidelity audit findings described below.

<AUDIT_FINDINGS>
{json.dumps(audit_result, indent=2)}
</AUDIT_FINDINGS>

<PRIMARY_SOURCE>
{source_text}
</PRIMARY_SOURCE>

<ORIGINAL_SCRIPT>
{json.dumps(script_dict, indent=2)}
</ORIGINAL_SCRIPT>

Return the corrected PodcastScriptResponse JSON now.
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": key}

    schema_dict = {
        "type": "OBJECT",
        "properties": {
            "episode_title": {"type": "STRING"},
            "episode_description": {"type": "STRING"},
            "source_title": {"type": "STRING"},
            "segments": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "order": {"type": "INTEGER"},
                        "heading": {"type": "STRING"},
                        "narration": {"type": "STRING"},
                    },
                    "required": ["order", "heading", "narration"],
                },
            },
            "warnings": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["episode_title", "episode_description", "segments", "warnings"],
    }

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": settings.GEMINI_MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
            "responseSchema": schema_dict,
        },
    }

    t0 = datetime.now(UTC)
    interaction_recorded = False
    try:
        from herald.concurrency import get_semaphores
        with get_semaphores().script, httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload, headers=headers)

        t1 = datetime.now(UTC)
        req_id = _extract_request_id(resp)

        if resp.status_code == 200:
            result_json = resp.json()
            candidates = result_json.get("candidates", [])
            if candidates:
                raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                p_tok, c_tok, t_tok = _extract_tokens(result_json)
                repair_obj = PodcastScriptResponse(**json.loads(raw_text))
                _record_gemini_interaction(
                    job_id=job_id,
                    model=model,
                    operation="fidelity_repair",
                    started_at=t0,
                    completed_at=t1,
                    success=True,
                    http_status=resp.status_code,
                    input_chars=len(prompt),
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    provider_request_id=req_id,
                )
                interaction_recorded = True
                return repair_obj

        _record_gemini_interaction(
            job_id=job_id,
            model=model,
            operation="fidelity_repair",
            started_at=t0,
            completed_at=t1,
            success=False,
            http_status=resp.status_code,
            input_chars=len(prompt),
            error=f"HTTP {resp.status_code}: {resp.text}",
            provider_request_id=req_id,
        )
        interaction_recorded = True
    except Exception as e:
        if not interaction_recorded:
            t1 = datetime.now(UTC)
            _record_gemini_interaction(
                job_id=job_id,
                model=model,
                operation="fidelity_repair",
                started_at=t0,
                completed_at=t1,
                success=False,
                input_chars=len(prompt),
                error=e,
            )
            interaction_recorded = True
        raise GeminiError(f"Failed to repair script fidelity: {e}")

    raise GeminiError("Failed to repair script fidelity.")

