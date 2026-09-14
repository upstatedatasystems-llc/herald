"""
Authoritative Extraction Recovery Ladder and Fallback Classifier for Herald.

Provides:
1. Fallback eligibility classification (centralized).
2. Conservative same-publisher URL recovery with domain and slug matching.
3. SSRF re-validation for all candidate recovered URLs.
4. Provider-neutral extraction fallback resolution (independent of user scripting provider).
5. Truthful telemetry and observability structures.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from herald.extraction.url_extractor import (
    ArticleExtractionError,
    ArticleNotFoundError,
    BlockReason,
    DNSResolutionError,
    InsufficientContentError,
    SSRFVulnerabilityError,
    validate_url_host,
)

logger = logging.getLogger("herald.extraction.recovery")

# Multi-part public suffixes for registrable domain extraction
_KNOWN_MULTI_SUFFIXES = {
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.nz", "net.nz", "org.nz",
    "co.jp", "ne.jp", "ac.jp",
    "co.za", "org.za",
    "com.br", "net.br", "org.br",
    "com.mx", "org.mx",
}


def get_registrable_domain(url: str) -> str:
    """
    Extract the effective registrable publisher domain (eTLD+1) from a URL.
    Handles subdomains, www prefix, and common multi-part suffixes (e.g. co.uk).
    Returns lowercased registrable domain, or empty string on error.
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().strip()
    except Exception:
        return ""

    if not hostname:
        return ""

    if hostname.startswith("www."):
        hostname = hostname[4:]

    parts = hostname.split(".")
    if len(parts) <= 2:
        return hostname

    # Check for known two-part TLDs (e.g. example.co.uk -> co.uk)
    potential_tld = f"{parts[-2]}.{parts[-1]}"
    if potential_tld in _KNOWN_MULTI_SUFFIXES:
        if len(parts) >= 3:
            return f"{parts[-3]}.{potential_tld}"
        return hostname

    return f"{parts[-2]}.{parts[-1]}"


def is_same_publisher(url_a: str, url_b: str) -> bool:
    """
    Strict check that two URLs belong to the identical registrable publisher domain.
    Never accepts cross-publisher substitutions.
    """
    dom_a = get_registrable_domain(url_a)
    dom_b = get_registrable_domain(url_b)
    if not dom_a or not dom_b:
        return False
    return dom_a == dom_b


def _extract_slug_keywords(url: str) -> set[str]:
    """Extract significant alphabetic keywords from URL path and query."""
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
    except Exception:
        return set()

    # Split on non-alphanumeric
    words = re.findall(r"[a-z0-9]{3,}", path)
    # Filter out common stop-words in slugs
    stops = {"html", "htm", "php", "asp", "aspx", "article", "post", "news", "story", "index"}
    return {w for w in words if w not in stops}


def check_slug_similarity(orig_url: str, cand_url: str, min_overlap_ratio: float = 0.5) -> bool:
    """
    Verify strong slug / path similarity between the original URL and candidate URL.
    Requires at least `min_overlap_ratio` of the original keywords to be present in the candidate.
    """
    orig_words = _extract_slug_keywords(orig_url)
    if not orig_words:
        # If original URL had no meaningful slug keywords, do not accept guessed URL
        return False

    cand_words = _extract_slug_keywords(cand_url)
    if not cand_words:
        return False

    intersection = orig_words.intersection(cand_words)
    overlap = len(intersection) / len(orig_words)
    return overlap >= min_overlap_ratio


def validate_and_sanitize_recovered_url(candidate_url: str, original_url: str) -> str:
    """
    Thoroughly validate candidate recovered URL against security and domain rules:
    1. Scheme check (HTTP/HTTPS only).
    2. Same-publisher registrable domain check.
    3. Strong slug/path similarity check.
    4. Mandatory full SSRF host resolution checks (no loopback, private, link-local, multicast).
    Returns the validated clean URL or raises ValueError / SSRFVulnerabilityError.
    """
    if not candidate_url or not isinstance(candidate_url, str):
        raise ValueError("Candidate URL must be a non-empty string.")

    cand_clean = candidate_url.strip()

    # 1. Scheme check
    parsed = urlparse(cand_clean)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"Prohibited scheme for recovered URL: {parsed.scheme}")

    # 2. Same-publisher check
    if not is_same_publisher(original_url, cand_clean):
        raise ValueError(
            f"Cross-publisher candidate rejected: '{cand_clean}' does not match original domain '{original_url}'"
        )

    # 3. Slug similarity check
    if not check_slug_similarity(original_url, cand_clean):
        raise ValueError(
            f"Weak/ambiguous candidate rejected: '{cand_clean}' lacks sufficient slug similarity to '{original_url}'"
        )

    # 4. Mandatory full SSRF / host validation
    validate_url_host(cand_clean)

    return cand_clean


def classify_extraction_failure(error: Exception, url: str) -> dict[str, Any]:
    """
    Authoritative classification of an extraction failure to decide fallback eligibility.
    Returns:
        {
            "category": str,
            "fallback_eligible": bool,
            "block_reason": str | None,
            "safe_detail": str,
        }
    """
    from herald.services.redaction import sanitize_error

    _, safe_msg = sanitize_error(error)

    # Security Failures: MUST REMAIN TERMINAL — NEVER FALLBACK
    if isinstance(error, SSRFVulnerabilityError):
        return {
            "category": "SSRF_PROTECTION",
            "fallback_eligible": False,
            "block_reason": None,
            "safe_detail": safe_msg,
        }

    err_str = str(error).lower()
    if (
        "ssrf" in err_str
        or "private ip" in err_str
        or "prohibited ip" in err_str
        or "loopback" in err_str
        or "localhost" in err_str
        or "security violation" in err_str
    ):
        return {
            "category": "SSRF_PROTECTION",
            "fallback_eligible": False,
            "block_reason": None,
            "safe_detail": safe_msg,
        }

    # Payload-size safety violations are security limits — never fallback
    if "maximum limit" in err_str or "exceeds maximum limit" in err_str or "response size exceeds" in err_str:
        return {
            "category": "PAYLOAD_TOO_LARGE",
            "fallback_eligible": False,
            "block_reason": None,
            "safe_detail": safe_msg,
        }

    # DNS Resolution Failures (domain does not exist / unresolvable host)
    if isinstance(error, DNSResolutionError) or "dns lookup failed" in err_str or "gaierror" in err_str:
        return {
            "category": "DNS_RESOLUTION_ERROR",
            "fallback_eligible": False,
            "block_reason": None,
            "safe_detail": safe_msg,
        }

    # HTTP 404 Public Article Not Found -> Eligible for same-publisher / canonical recovery
    if isinstance(error, ArticleNotFoundError) or "404 not found" in err_str or "status code: 404" in err_str:
        return {
            "category": "ARTICLE_NOT_FOUND",
            "fallback_eligible": True,
            "block_reason": "HTTP_404_NOT_FOUND",
            "safe_detail": safe_msg,
        }

    # Insufficient article text extracted (< 100 chars) -> Eligible for URL Context fallback
    if isinstance(error, InsufficientContentError) or "insufficient article text" in err_str:
        return {
            "category": "INSUFFICIENT_CONTENT",
            "fallback_eligible": True,
            "block_reason": "INSUFFICIENT_CONTENT",
            "safe_detail": safe_msg,
        }

    # Publisher Access Blocks (401, 403, Cloudflare, Paywall, Captcha, Interstitial, 429)
    from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

    if isinstance(error, SourceAccessBlockedError):
        block_reason = getattr(error, "block_reason", BlockReason.PUBLIC_RETRIEVAL_BLOCK)
        is_eligible = block_reason in (
            BlockReason.PUBLIC_RETRIEVAL_BLOCK,
            BlockReason.RATE_LIMITED,
            BlockReason.INTERSTITIAL,
            BlockReason.CAPTCHA,
        )
        return {
            "category": "SOURCE_ACCESS_BLOCKED",
            "fallback_eligible": is_eligible,
            "block_reason": block_reason,
            "safe_detail": safe_msg,
        }

    # Generic ArticleExtractionError with non-200 or interstitial
    if isinstance(error, ArticleExtractionError):
        # Cloudflare or public bot challenge or 403 on public sites
        if any(code in err_str for code in ("403", "cloudflare", "bot challenge", "interstitial", "just a moment", "turnstile", "captcha")):
            reason = BlockReason.PUBLIC_RETRIEVAL_BLOCK
            if "interstitial" in err_str or "just a moment" in err_str:
                reason = BlockReason.INTERSTITIAL
            elif "captcha" in err_str or "turnstile" in err_str:
                reason = BlockReason.CAPTCHA
            return {
                "category": "SOURCE_ACCESS_BLOCKED",
                "fallback_eligible": True,
                "block_reason": reason,
                "safe_detail": safe_msg,
            }
        return {
            "category": getattr(error, "error_category", "EXTRACTION_FAILURE"),
            "fallback_eligible": False,
            "block_reason": None,
            "safe_detail": safe_msg,
        }

    return {
        "category": "EXTRACTION_FAILURE",
        "fallback_eligible": False,
        "block_reason": None,
        "safe_detail": safe_msg,
    }


def find_extraction_fallback_provider() -> tuple[str | None, Any | None]:
    """
    Find any configured AI provider with 'url_context_extraction' capability,
    independent of the user's selected scripting provider preference.
    Checks circuit breaker status before returning.
    Returns (provider_id, provider_instance) or (None, None).
    """
    try:
        from herald.ai.circuit_breaker import is_circuit_breaker_active
    except ImportError:
        is_circuit_breaker_active = lambda p: (False, None)

    from herald.ai.registry import create_provider, list_descriptors

    for desc in list_descriptors():
        if desc.is_configured() and getattr(desc.capabilities, "url_context_extraction", False):
            p_id = desc.provider_id
            active, reason = is_circuit_breaker_active(p_id)
            if active:
                logger.info(f"Skipping extraction fallback provider '{p_id}': circuit breaker active ({reason})")
                continue
            try:
                inst = create_provider(p_id)
                return p_id, inst
            except Exception as e:
                logger.warning(f"Failed to instantiate extraction fallback provider '{p_id}': {e}")
                continue

    return None, None


def find_grounded_discovery_provider() -> tuple[str | None, Any | None]:
    """
    Find any configured AI provider with 'research_grounding' capability,
    independent of user scripting provider preference.
    Checks circuit breaker status before returning.
    Returns (provider_id, provider_instance) or (None, None).
    """
    try:
        from herald.ai.circuit_breaker import is_circuit_breaker_active
    except ImportError:
        is_circuit_breaker_active = lambda p: (False, None)

    from herald.ai.registry import create_provider, list_descriptors

    for desc in list_descriptors():
        if desc.is_configured() and getattr(desc.capabilities, "research_grounding", False):
            p_id = desc.provider_id
            active, reason = is_circuit_breaker_active(p_id)
            if active:
                logger.info(f"Skipping grounded discovery provider '{p_id}': circuit breaker active ({reason})")
                continue
            try:
                inst = create_provider(p_id)
                return p_id, inst
            except Exception as e:
                logger.warning(f"Failed to instantiate discovery provider '{p_id}': {e}")
                continue

    return None, None


def discover_same_publisher_replacement_url(
    original_url: str,
    job_id: str | None = None,
) -> str | None:
    """
    Bounded same-publisher URL discovery for HTTP 404 / stale URLs.
    Performs one search query using an available configured provider with research/search grounding.
    Candidate URLs must strictly come from grounded/search source metadata (not generated prose).
    Filters candidates by:
    - Same registrable publisher domain
    - Strong slug / path keyword similarity
    - HTTP/HTTPS only
    - Full SSRF revalidation
    - Ambiguity rejection: if multiple distinct candidates pass, rejects to avoid guessing.
    Returns the validated replacement URL or None.
    """
    if not original_url or not isinstance(original_url, str):
        return None

    domain = get_registrable_domain(original_url)
    if not domain:
        return None

    slug_words = _extract_slug_keywords(original_url)
    if not slug_words:
        logger.info(f"Original URL '{original_url}' lacks slug keywords; skipping discovery.")
        return None

    prov_id, prov = find_grounded_discovery_provider()
    if not prov:
        logger.info(f"No configured provider with research_grounding available for discovery of {original_url}.")
        return None

    search_terms = " ".join(sorted(slug_words))
    discovery_query = f"site:{domain} {search_terms}"
    logger.info(f"Attempting grounded same-publisher discovery for '{original_url}' via {prov_id}: '{discovery_query}'")

    try:
        grounded_data = prov.generate_grounded_research(
            source_text=f"Find the original article published on {domain} matching: {original_url}\nSearch query: {discovery_query}",
            research_depth="low",
            job_id=job_id,
        )
    except Exception as e:
        logger.warning(f"Grounded discovery call to {prov_id} failed: {e}")
        return None

    if not isinstance(grounded_data, dict):
        return None

    # Strictly extract candidates from search/grounding metadata, NOT generated prose!
    sources = list(grounded_data.get("research_sources") or grounded_data.get("sources") or [])
    if not sources and "grounding_metadata" in grounded_data:
        gm = grounded_data["grounding_metadata"]
        chunks = gm.get("groundingChunks") or gm.get("grounding_chunks") or []
        for c in chunks:
            web = c.get("web") or {}
            u = web.get("uri") or web.get("url")
            if u:
                sources.append({"url": u, "title": web.get("title")})

    candidate_urls: list[str] = []
    for s in sources:
        cand = s.get("url") if isinstance(s, dict) else (s if isinstance(s, str) else None)
        if not cand or not isinstance(cand, str):
            continue
        try:
            valid_cand = validate_and_sanitize_recovered_url(cand, original_url)
            if valid_cand not in candidate_urls:
                candidate_urls.append(valid_cand)
        except Exception as val_err:
            logger.debug(f"Discovered candidate '{cand}' failed recovery validation: {val_err}")

    if len(candidate_urls) == 1:
        logger.info(f"Grounded same-publisher discovery succeeded: '{candidate_urls[0]}' for original '{original_url}'")
        return candidate_urls[0]
    elif len(candidate_urls) > 1:
        logger.warning(
            f"Grounded discovery returned ambiguous candidates for '{original_url}': {candidate_urls}. Rejecting to prevent guessing."
        )
        return None
    else:
        logger.info(f"Grounded discovery found no valid same-publisher replacement for '{original_url}'.")
        return None
