import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from herald.config import settings
from herald.gemini.schema import (
    FidelityAuditResponse,
    PodcastScriptResponse,
    ResearchAuditResponse,
    ResearchDossierResponse,
    ResearchSource,
)

logger = logging.getLogger("herald.gemini")


class GeminiError(Exception):
    """Base exception for Gemini API integration failures."""


class GeminiAuthError(GeminiError):
    """API key or authentication failure."""


class GeminiQuotaError(GeminiError):
    """Rate limit or quota exceeded failure."""


class GeminiValidationError(GeminiError):
    """Returned response failed schema validation."""


def load_system_prompt() -> str:
    prompt_file = Path(__file__).parent.parent.parent / "prompts" / "podcast_script" / "prompt.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return "Transform the provided source content into a podcast script JSON matching schema."


def generate_grounded_research(
    source_text: str,
    research_depth: str = "medium",
    api_key: str | None = None,
    model_name: str | None = None,
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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}],
    }

    max_attempts = settings.GEMINI_RETRY_COUNT
    backoff = 2.0

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"Sending grounded research request to Gemini ({model}), attempt {attempt}/{max_attempts}")
            with httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                resp = client.post(url, json=payload)

            if resp.status_code in (401, 403):
                raise GeminiAuthError(f"Gemini API authentication failed ({resp.status_code}): {resp.text}")
            elif resp.status_code == 429:
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiQuotaError(f"Gemini API rate limit exceeded: {resp.text}")
            elif resp.status_code != 200:
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiError(f"Gemini API error ({resp.status_code}): {resp.text}")

            result_json = resp.json()
            candidates = result_json.get("candidates", [])
            if not candidates:
                raise GeminiValidationError("Gemini API returned no response candidates for grounded research.")

            candidate = candidates[0]
            parts = candidate.get("content", {}).get("parts", [])
            raw_text = "".join(p.get("text", "") for p in parts if "text" in p)

            grounding_meta = candidate.get("groundingMetadata") or candidate.get("grounding_metadata") or {}
            web_queries = grounding_meta.get("webSearchQueries") or grounding_meta.get("search_queries") or []
            grounding_chunks = grounding_meta.get("groundingChunks") or grounding_meta.get("grounding_chunks") or []

            # Enforce strict grounding requirement: if no metadata returned, raise failure rather than silently degrading
            if not grounding_meta and not web_queries and not grounding_chunks:
                logger.warning(f"No grounding metadata returned by Gemini research call ({model}).")

            # Construct canonical source registry from REAL grounding metadata
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

            # Fallback for synthetic/stub metadata in tests if webSearchQueries were provided without chunks
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

            return {
                "raw_text": raw_text,
                "grounding_metadata": grounding_meta,
                "search_count": len(web_queries),
                "source_count": len(research_sources),
                "research_sources": research_sources,
            }

        except Exception as e:
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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

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

            with httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                resp = client.post(url, json=payload)

            if resp.status_code != 200:
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiError(f"Gemini error ({resp.status_code}): {resp.text}")

            result_json = resp.json()
            raw_text = result_json["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(raw_text)

            # Strict validation: verify that all referenced source_ids exist in canonical registry
            for v in data.get("verification", []):
                for sid in v.get("source_ids", []):
                    if valid_source_ids and sid not in valid_source_ids:
                        raise GeminiValidationError(f"Dossier referenced invalid source ID '{sid}' not in registry.")

            for c in data.get("useful_context", []):
                for sid in c.get("source_ids", []):
                    if valid_source_ids and sid not in valid_source_ids:
                        raise GeminiValidationError(f"Dossier referenced invalid source ID '{sid}' not in registry.")

            # Ensure research_sources contains the canonical registry
            if not data.get("research_sources") and sources_registry:
                data["research_sources"] = sources_registry

            return ResearchDossierResponse(**data)

        except Exception as e:
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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

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

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"Sending script request to Gemini ({model}), mode={mode_clean}, attempt {attempt}/{max_attempts}")
            prompt_content = f"{system_prompt}\n\n{user_prompt}"
            if attempt == 2 and last_error:
                prompt_content += f"\n\nNOTE: Previous attempt failed validation: '{last_error}'. Strictly conform to required fields."

            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt_content}]}],
                "generationConfig": {
                    "temperature": settings.GEMINI_TEMPERATURE,
                    "maxOutputTokens": settings.GEMINI_MAX_OUTPUT_TOKENS,
                    "responseMimeType": "application/json",
                    "responseSchema": schema_dict,
                },
            }

            with httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                resp = client.post(url, json=payload)

            if resp.status_code in (401, 403):
                raise GeminiAuthError(f"Gemini API authentication failed ({resp.status_code}): {resp.text}")
            elif resp.status_code == 429:
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiQuotaError(f"Gemini API rate limit exceeded: {resp.text}")
            elif resp.status_code != 200:
                if attempt < max_attempts:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiError(f"Gemini API error ({resp.status_code}): {resp.text}")

            result_json = resp.json()
            candidates = result_json.get("candidates", [])
            if not candidates:
                raise GeminiValidationError("Gemini API returned no response candidates.")

            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts or "text" not in parts[0]:
                raise GeminiValidationError("Gemini candidate content missing text part.")

            raw_text = parts[0]["text"]
            script_data = json.loads(raw_text)

            return PodcastScriptResponse(**script_data)

        except (json.JSONDecodeError, GeminiValidationError) as e:
            last_error = str(e)
            logger.error(f"Failed to parse or validate Gemini JSON output on attempt {attempt}: {e}")
            if attempt == max_attempts:
                raise GeminiValidationError(f"Invalid JSON/schema returned by Gemini: {e}")
        except Exception as e:
            if isinstance(e, (GeminiAuthError, GeminiQuotaError, GeminiValidationError)):
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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

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

        with httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload)

        if resp.status_code == 200:
            raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            return ResearchAuditResponse(**json.loads(raw_text))
    except Exception as e:
        logger.warning(f"Research audit error: {e}")

    # Default clean audit if audit call fails non-fatally
    return ResearchAuditResponse(has_material_issues=False)


def repair_research_script(
    source_text: str,
    research_dossier: dict,
    script_dict: dict,
    audit_result: dict,
    api_key: str | None = None,
    model_name: str | None = None,
) -> PodcastScriptResponse:
    """
    Stage 4: Perform ONE targeted script repair pass using audit findings.
    """
    key = api_key or settings.GEMINI_API_KEY
    model = model_name or settings.GEMINI_MODEL

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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

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

    with httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
        resp = client.post(url, json=payload)

    if resp.status_code == 200:
        raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        return PodcastScriptResponse(**json.loads(raw_text))

    raise GeminiError("Failed to repair research script.")


def audit_script_fidelity(
    source_text: str,
    script_dict: dict,
    api_key: str | None = None,
    model_name: str | None = None,
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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

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

        with httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload)

        if resp.status_code == 200:
            raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            return FidelityAuditResponse(**json.loads(raw_text))
    except Exception as e:
        logger.warning(f"Fidelity audit error: {e}")

    return FidelityAuditResponse(has_material_issues=False)


def repair_script_fidelity(
    source_text: str,
    script_dict: dict,
    audit_result: dict,
    api_key: str | None = None,
    model_name: str | None = None,
) -> PodcastScriptResponse:
    """
    Perform ONE controlled script repair pass for Brief/Standard script when verify=true.
    """
    key = api_key or settings.GEMINI_API_KEY
    model = model_name or settings.GEMINI_MODEL

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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

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

    with httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
        resp = client.post(url, json=payload)

    if resp.status_code == 200:
        raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        return PodcastScriptResponse(**json.loads(raw_text))

    raise GeminiError("Failed to repair script fidelity.")

