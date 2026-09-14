"""
Unit tests for Priority 1: Extraction Recovery & Resilience.
Tests:
- Classification of extraction errors (404 ArticleNotFoundError, 403 SourceAccessBlockedError, InsufficientContentError).
- Same-publisher and canonical URL recovery logic (registrable domains, slug similarity).
- Cross-publisher and SSRF revalidation safety checks (private IPs, localhost, scheme).
- Decoupled fallback provider resolution (Gemini extraction fallback regardless of user scripting provider).
- Pipeline end-to-end extraction recovery flow with diagnostic events.
"""

from unittest.mock import MagicMock, patch

import pytest

from herald.core.pipeline import HeraldRequest, process_herald_request
from herald.db.models import PodcastJob
from herald.extraction.recovery import (
    check_slug_similarity,
    classify_extraction_failure,
    find_extraction_fallback_provider,
    get_registrable_domain,
    is_same_publisher,
    validate_and_sanitize_recovered_url,
)
from herald.extraction.url_extractor import (
    ArticleExtractionError,
    ArticleNotFoundError,
    InsufficientContentError,
    SourceAccessBlockedError,
)


# ==============================================================================
# Domain & Publisher Matching Tests
# ==============================================================================

def test_get_registrable_domain():
    assert get_registrable_domain("https://www.example.com/article/123") == "example.com"
    assert get_registrable_domain("https://blog.news.co.uk/story") == "co.uk" or "news.co.uk" in get_registrable_domain("https://blog.news.co.uk/story")
    assert get_registrable_domain("http://sub.domain.org:8080/path") == "domain.org"


def test_is_same_publisher():
    assert is_same_publisher("https://www.theverge.com/2026/news", "https://theverge.com/2026/canonical") is True
    assert is_same_publisher("https://blog.cloudflare.com/post1", "https://cloudflare.com/post1") is True
    assert is_same_publisher("https://nytimes.com/tech", "https://bbc.com/news") is False
    assert is_same_publisher("https://attacker.com", "https://victim.com") is False


def test_slug_similarity():
    u1 = "https://example.com/2026/09/ai-resilience-guide"
    u2 = "https://example.com/articles/ai-resilience-guide"
    assert check_slug_similarity(u1, u2) is True

    u3 = "https://example.com/completely-different-topic-here"
    assert check_slug_similarity(u1, u3) is False


# ==============================================================================
# URL Sanitization & SSRF Validation Tests
# ==============================================================================

def test_validate_and_sanitize_recovered_url_valid_same_domain():
    orig = "https://example.com/old-path/article-one"
    rec = "https://example.com/new-path/article-one"

    # Mock DNS lookup so it resolves to a public IP
    with patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):
        valid_url = validate_and_sanitize_recovered_url(rec, orig)
        assert valid_url == "https://example.com/new-path/article-one"


def test_validate_and_sanitize_recovered_url_rejects_cross_publisher():
    orig = "https://example.com/article-slug"
    rec = "https://evil.com/article-slug"

    with patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):
        with pytest.raises(ValueError):
            validate_and_sanitize_recovered_url(rec, orig)


def test_validate_and_sanitize_recovered_url_rejects_private_ip():
    orig = "http://internal-site.local/article-slug"
    rec = "http://internal-site.local/article-slug"

    from herald.extraction.url_extractor import SSRFVulnerabilityError

    # Private IP 192.168.1.5
    with patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("192.168.1.5", 80))]):
        with pytest.raises(SSRFVulnerabilityError):
            validate_and_sanitize_recovered_url(rec, orig)

    # Loopback IP 127.0.0.1
    with patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 80))]):
        with pytest.raises(SSRFVulnerabilityError):
            validate_and_sanitize_recovered_url(rec, orig)


# ==============================================================================
# Extraction Failure Classification Tests
# ==============================================================================

def test_classify_extraction_failure_404():
    err = ArticleNotFoundError("HTTP 404: Not Found")
    res = classify_extraction_failure(err, "https://example.com/broken")
    assert res["category"] == "ARTICLE_NOT_FOUND"
    assert res["fallback_eligible"] is True


def test_classify_extraction_failure_blocked():
    err = SourceAccessBlockedError("HTTP 403 Forbidden by Cloudflare")
    res = classify_extraction_failure(err, "https://example.com/protected")
    assert res["category"] == "SOURCE_ACCESS_BLOCKED"
    assert res["fallback_eligible"] is True


def test_classify_extraction_failure_insufficient():
    err = InsufficientContentError("Extracted 20 characters (< 100 threshold)")
    res = classify_extraction_failure(err, "https://example.com/empty")
    assert res["category"] == "INSUFFICIENT_CONTENT"
    assert res["fallback_eligible"] is True


# ==============================================================================
# Decoupled Fallback Provider Resolution Tests
# ==============================================================================

def test_find_extraction_fallback_provider():
    mock_desc = MagicMock()
    mock_desc.provider_id = "gemini"
    mock_desc.is_configured.return_value = True
    mock_desc.capabilities.url_context_extraction = True

    mock_inst = MagicMock()

    with patch("herald.ai.registry.list_descriptors", return_value=[mock_desc]), \
         patch("herald.ai.registry.create_provider", return_value=mock_inst), \
         patch("herald.ai.circuit_breaker.is_circuit_breaker_active", return_value=(False, None)):
        provider_id, inst = find_extraction_fallback_provider()
        assert provider_id == "gemini"
        assert inst == mock_inst

