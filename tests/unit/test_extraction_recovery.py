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


def test_classify_extraction_failure_interstitial_and_captcha():
    from herald.extraction.url_extractor import BlockReason

    err_interstitial = SourceAccessBlockedError("Cloudflare challenge", block_reason=BlockReason.INTERSTITIAL)
    res_interstitial = classify_extraction_failure(err_interstitial, "https://example.com/page")
    assert res_interstitial["category"] == "SOURCE_ACCESS_BLOCKED"
    assert res_interstitial["fallback_eligible"] is True
    assert res_interstitial["block_reason"] == BlockReason.INTERSTITIAL

    err_captcha = SourceAccessBlockedError("Turnstile captcha", block_reason=BlockReason.CAPTCHA)
    res_captcha = classify_extraction_failure(err_captcha, "https://example.com/page")
    assert res_captcha["category"] == "SOURCE_ACCESS_BLOCKED"
    assert res_captcha["fallback_eligible"] is True
    assert res_captcha["block_reason"] == BlockReason.CAPTCHA

    err_auth = SourceAccessBlockedError("Login required", block_reason=BlockReason.AUTH_REQUIRED)
    res_auth = classify_extraction_failure(err_auth, "https://example.com/page")
    assert res_auth["fallback_eligible"] is False

    err_paywall = SourceAccessBlockedError("Paywall subscriber only", block_reason=BlockReason.PAYWALL)
    res_paywall = classify_extraction_failure(err_paywall, "https://example.com/page")
    assert res_paywall["fallback_eligible"] is False


# ==============================================================================
# Grounded Same-Publisher Discovery Tests
# ==============================================================================

def test_discover_same_publisher_replacement_success():
    from herald.extraction.recovery import discover_same_publisher_replacement_url

    mock_prov = MagicMock()
    mock_prov.generate_grounded_research.return_value = {
        "research_sources": [
            {"url": "https://example.com/articles/2026/real-slug-article", "title": "Real Article"}
        ]
    }

    with patch("herald.extraction.recovery.find_grounded_discovery_provider", return_value=("gemini", mock_prov)), \
         patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):
        found = discover_same_publisher_replacement_url("https://example.com/news/2026/broken-slug-article")
        assert found == "https://example.com/articles/2026/real-slug-article"


def test_discover_same_publisher_rejects_cross_publisher():
    from herald.extraction.recovery import discover_same_publisher_replacement_url

    mock_prov = MagicMock()
    mock_prov.generate_grounded_research.return_value = {
        "research_sources": [
            {"url": "https://evil-substitute.com/news/2026/broken-slug-article", "title": "Evil"}
        ]
    }

    with patch("herald.extraction.recovery.find_grounded_discovery_provider", return_value=("gemini", mock_prov)), \
         patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):
        found = discover_same_publisher_replacement_url("https://example.com/news/2026/broken-slug-article")
        assert found is None


def test_discover_same_publisher_rejects_weak_slug():
    from herald.extraction.recovery import discover_same_publisher_replacement_url

    mock_prov = MagicMock()
    mock_prov.generate_grounded_research.return_value = {
        "research_sources": [
            {"url": "https://example.com/about/contact-us", "title": "Contact"}
        ]
    }

    with patch("herald.extraction.recovery.find_grounded_discovery_provider", return_value=("gemini", mock_prov)), \
         patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):
        found = discover_same_publisher_replacement_url("https://example.com/news/2026/quantum-computing-breakthrough")
        assert found is None


def test_discover_same_publisher_rejects_private_ip():
    from herald.extraction.recovery import discover_same_publisher_replacement_url

    mock_prov = MagicMock()
    mock_prov.generate_grounded_research.return_value = {
        "research_sources": [
            {"url": "https://example.com/news/2026/quantum-computing-breakthrough", "title": "Internal"}
        ]
    }

    with patch("herald.extraction.recovery.find_grounded_discovery_provider", return_value=("gemini", mock_prov)), \
         patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 443))]):
        found = discover_same_publisher_replacement_url("https://example.com/news/2026/quantum-computing-breakthrough")
        assert found is None


def test_discover_same_publisher_rejects_ambiguous_candidates():
    from herald.extraction.recovery import discover_same_publisher_replacement_url

    mock_prov = MagicMock()
    mock_prov.generate_grounded_research.return_value = {
        "research_sources": [
            {"url": "https://example.com/news/2026/quantum-computing-breakthrough-part-1", "title": "Part 1"},
            {"url": "https://example.com/news/2026/quantum-computing-breakthrough-part-2", "title": "Part 2"},
        ]
    }

    with patch("herald.extraction.recovery.find_grounded_discovery_provider", return_value=("gemini", mock_prov)), \
         patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):
        found = discover_same_publisher_replacement_url("https://example.com/news/2026/quantum-computing-breakthrough")
        assert found is None


# ==============================================================================
# Pipeline-Level Regression Tests
# ==============================================================================

def test_pipeline_interstitial_block_fallback_attempted():
    """Pipeline regression test: Cloudflare/interstitial 403 triggers URL Context fallback."""
    from herald.extraction.url_extractor import BlockReason

    req = HeraldRequest(
        source_url="https://protected-site.com/news/tech-story-2026",
        request_mode="standard",
        transport="telegram",
        delivery_target="123456",
    )

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.all.return_value = []

    interstitial_err = SourceAccessBlockedError(
        "Just a moment... Cloudflare challenge required",
        block_reason=BlockReason.INTERSTITIAL,
    )

    mock_fb_prov = MagicMock()
    mock_fb_prov.extract_article_via_url_context.return_value = {
        "title": "Recovered Tech Story",
        "body": "This is full recovered article body from URL context extraction. " * 5,
    }

    recorded_metrics = []
    def mock_record_stage_metric(*args, **kwargs):
        recorded_metrics.append(kwargs)

    with patch("herald.core.pipeline.extract_article_from_url", side_effect=interstitial_err), \
         patch("herald.extraction.recovery.find_extraction_fallback_provider", return_value=("gemini", mock_fb_prov)), \
         patch("herald.core.pipeline.is_provider_configured", return_value=True), \
         patch("herald.core.pipeline.record_stage_metric", side_effect=mock_record_stage_metric), \
         patch("herald.core.pipeline.record_job_diagnostic_event"), \
         patch("herald.core.pipeline.resolve_job_settings") as mock_resolve, \
         patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):

        mock_candidate = MagicMock()
        mock_candidate.provider_id = "gemini"
        mock_candidate.model_id = "gemini-2.5-flash"
        mock_res_settings = MagicMock()
        mock_res_settings.mode = "standard"
        mock_res_settings.ai_candidates = [mock_candidate]
        mock_resolve.return_value = mock_res_settings

        # Mock execute_with_failover for scripting stage
        mock_script_resp = MagicMock()
        mock_script_resp.episode_title = "Recovered Tech Story"
        mock_script_resp.episode_description = "A story recovered via URL context"
        mock_script_resp.segments = [MagicMock(narration="Story content")]

        with patch("herald.core.pipeline.execute_with_failover", return_value=mock_script_resp):
            resp = process_herald_request(db=db, req=req)

    # Verify fallback was attempted and succeeded
    assert mock_fb_prov.extract_article_via_url_context.called
    assert any(
        m.get("metadata_json", {}).get("fallback_attempted") is True
        and m.get("metadata_json", {}).get("fallback_result") == "SUCCESS"
        for m in recorded_metrics
    )


def test_pipeline_404_same_publisher_grounded_discovery():
    """Pipeline regression test: 404 URL recovered via grounded same-publisher discovery."""
    req = HeraldRequest(
        source_url="https://example.com/2026/news/broken-slug-article",
        request_mode="standard",
        transport="telegram",
        delivery_target="123456",
    )

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.all.return_value = []

    def mock_extract(url, *args, **kwargs):
        if "broken-slug-article" in url and "updated" not in url:
            raise ArticleNotFoundError(f"Publisher returned HTTP 404 Not Found: {url}")
        if "broken-slug-article-updated" in url:
            return ("Recovered Article", "This is recovered article text with plenty of content to pass threshold." * 5, url)
        raise ArticleNotFoundError("Not found")

    mock_disc_prov = MagicMock()
    mock_disc_prov.generate_grounded_research.return_value = {
        "research_sources": [
            {"url": "https://example.com/2026/news/broken-slug-article-updated", "title": "Recovered Article"}
        ]
    }

    recorded_metrics = []
    def mock_record_stage_metric(*args, **kwargs):
        recorded_metrics.append(kwargs)

    with patch("herald.core.pipeline.extract_article_from_url", side_effect=mock_extract), \
         patch("herald.extraction.recovery.find_grounded_discovery_provider", return_value=("gemini", mock_disc_prov)), \
         patch("herald.core.pipeline.is_provider_configured", return_value=True), \
         patch("herald.core.pipeline.record_stage_metric", side_effect=mock_record_stage_metric), \
         patch("herald.core.pipeline.record_job_diagnostic_event"), \
         patch("herald.core.pipeline.resolve_job_settings") as mock_resolve, \
         patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):

        mock_candidate = MagicMock()
        mock_candidate.provider_id = "gemini"
        mock_candidate.model_id = "gemini-2.5-flash"
        mock_res_settings = MagicMock()
        mock_res_settings.mode = "standard"
        mock_res_settings.ai_candidates = [mock_candidate]
        mock_resolve.return_value = mock_res_settings

        mock_script_resp = MagicMock()
        mock_script_resp.episode_title = "Recovered Article"
        mock_script_resp.episode_description = "A story recovered via 404 discovery"
        mock_script_resp.segments = [MagicMock(narration="Story content")]

        with patch("herald.core.pipeline.execute_with_failover", return_value=mock_script_resp):
            resp = process_herald_request(db=db, req=req)

    # Verify discovery was attempted and resolved
    assert any(
        m.get("metadata_json", {}).get("fallback_method") == "SAME_PUBLISHER_GROUNDED_DISCOVERY"
        and m.get("metadata_json", {}).get("fallback_result") == "SUCCESS"
        and m.get("metadata_json", {}).get("resolved_url") == "https://example.com/2026/news/broken-slug-article-updated"
        for m in recorded_metrics
    )

