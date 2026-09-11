"""
Unit and integration tests for:
1. Pre-AI URL Context extraction failover tests (User Correction 27)
2. Request preflight validation and safe telemetry tests (User Correction 28)
"""

from unittest.mock import patch

import pytest

from herald.ai.errors import (
    AIChainExhaustedError,
    AIContextExceededError,
    AIRateLimitedError,
)
from herald.ai.failover import execute_with_failover, record_ai_preflight
from herald.db.models import PodcastJob


def create_job(chain: list[dict[str, str]], failover_index: int = 0) -> PodcastJob:
    return PodcastJob(
        id="test-job-url-ctx-456",
        transport="telegram",
        status="EXTRACTING",
        ai_provider=chain[0]["provider"] if chain else "gemini",
        ai_model=chain[0]["model"] if chain else "gemini-3.5-flash",
        ai_provider_chain_json=chain,
        ai_failover_index=failover_index,
    )


# ==============================================================================
# Pre-AI Extraction Failover Tests (User Correction 27)
# ==============================================================================

def test_url_context_primary_succeeds_scripting_remains_on_primary():
    """Test A: Primary supports URL Context and succeeds. Script generation remains on Primary."""
    chain = [
        {"provider": "gemini", "model": "gemini-3.5-flash"},  # supports url_context_extraction
        {"provider": "groq", "model": "groq/compound"},
    ]
    job = create_job(chain)
    calls = []

    def mock_url_ctx(provider_instance, attempt):
        calls.append(("url_context", provider_instance.provider_name))
        return {"title": "Article Title", "body": "Extracted article body"}

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        url_res = execute_with_failover(
            job,
            operation="url_context_extraction",
            execute_fn=mock_url_ctx,
            required_capability="url_context_extraction",
        )

    assert url_res["body"] == "Extracted article body"
    assert job.ai_failover_index == 0
    assert job.ai_effective_provider == "gemini"

    # Subsequent script generation must run on Primary (Gemini)
    def mock_script(provider_instance, attempt):
        calls.append(("script", provider_instance.provider_name))
        return "script-result"

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        script_res = execute_with_failover(
            job,
            operation="script_generation",
            execute_fn=mock_script,
        )

    assert script_res == "script-result"
    assert calls == [("url_context", "Gemini"), ("script", "Gemini")]


def test_url_context_primary_fails_secondary_succeeds_and_becomes_sticky():
    """
    Test B: Primary URL Context receives failover-eligible error.
    Secondary supports URL Context and succeeds.
    ai_failover_index becomes Secondary. Script generation begins with Secondary.
    """
    # Create two candidates that both support url_context_extraction (e.g. Gemini 3.5 and Gemini 2.5)
    chain = [
        {"provider": "gemini", "model": "gemini-3.5-flash"},
        {"provider": "gemini", "model": "gemini-2.5-flash"},
    ]
    job = create_job(chain)
    calls = []

    def mock_url_ctx(provider_instance, attempt):
        calls.append(("url_context", provider_instance.configured_model))
        if provider_instance.configured_model == "gemini-3.5-flash":
            raise AIRateLimitedError("Gemini 3.5 429 quota exhausted")
        return {"title": "Title", "body": "Body from 2.5"}

    with patch("herald.ai.failover.is_provider_configured", return_value=True), \
         patch("time.sleep", return_value=None):
        url_res = execute_with_failover(
            job,
            operation="url_context_extraction",
            execute_fn=mock_url_ctx,
            required_capability="url_context_extraction",
            max_same_provider_attempts=1,
        )

    assert url_res["body"] == "Body from 2.5"
    assert job.ai_failover_index == 1
    assert job.ai_effective_model == "gemini-2.5-flash"

    # Subsequent script generation begins with Secondary (candidate 1)
    def mock_script(provider_instance, attempt):
        calls.append(("script", provider_instance.configured_model))
        return "script-secondary"

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        script_res = execute_with_failover(
            job,
            operation="script_generation",
            execute_fn=mock_script,
        )

    assert script_res == "script-secondary"
    # Script generation did NOT call gemini-3.5-flash; it used sticky gemini-2.5-flash!
    assert ("script", "gemini-3.5-flash") not in calls
    assert ("script", "gemini-2.5-flash") in calls


def test_url_context_primary_lacks_capability_secondary_used():
    """
    Test C: Primary lacks URL Context (Groq). Secondary supports it (Gemini).
    Groq is skipped as UNSUPPORTED_CAPABILITY. Gemini becomes sticky.
    """
    chain = [
        {"provider": "groq", "model": "groq/compound"},  # url_context_extraction = False
        {"provider": "gemini", "model": "gemini-3.5-flash"},  # url_context_extraction = True
    ]
    job = create_job(chain)
    calls = []

    def mock_url_ctx(provider_instance, attempt):
        calls.append(("url_context", provider_instance.provider_name))
        return {"title": "Title", "body": "Gemini URL Body"}

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        res = execute_with_failover(
            job,
            operation="url_context_extraction",
            execute_fn=mock_url_ctx,
            required_capability="url_context_extraction",
        )

    assert res["body"] == "Gemini URL Body"
    assert calls == [("url_context", "Gemini")]
    assert job.ai_failover_index == 1
    assert job.ai_effective_provider == "gemini"


def test_url_context_no_candidate_supports_clean_capability_failure():
    """
    Test D: No candidate in snapshot supports URL Context.
    Clean capability failure; no provider outside snapshot is secretly called.
    """
    chain = [
        {"provider": "groq", "model": "groq/compound"},
        {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
    ]
    job = create_job(chain)
    calls = []

    def mock_url_ctx(provider_instance, attempt):
        calls.append(provider_instance.provider_name)
        return {}

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        with pytest.raises(AIChainExhaustedError) as exc_info:
            execute_with_failover(
                job,
                operation="url_context_extraction",
                execute_fn=mock_url_ctx,
                required_capability="url_context_extraction",
            )

    # Neither Groq nor Cloudflare was invoked for url_context (both skipped)
    assert calls == []
    assert "UNSUPPORTED_CAPABILITY" in str(exc_info.value)


def test_server_restart_after_url_context_failover_recovers_at_secondary():
    """
    Test E: Server restart after URL Context failed over to Secondary.
    Job in DB has ai_failover_index = 1. Execution resumes on Secondary.
    """
    chain = [
        {"provider": "gemini", "model": "gemini-3.5-flash"},
        {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
    ]
    # Restored job from DB with ai_failover_index = 1
    job = create_job(chain, failover_index=1)
    calls = []

    def mock_script(provider_instance, attempt):
        calls.append(provider_instance.provider_name)
        return "script-success"

    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        res = execute_with_failover(job, operation="script_generation", execute_fn=mock_script)

    assert res == "script-success"
    # Started directly at Cloudflare!
    assert calls == ["Cloudflare Workers AI"]


# ==============================================================================
# Request Preflight Tests (User Correction 28)
# ==============================================================================

def test_preflight_detects_known_context_overflow_before_invocation():
    """Known context overflow raises AIContextExceededError before provider request."""
    # Qwen 3.8 has known context limit of 32,768 tokens (~131,072 chars)
    giant_text = "word " * 50_000  # ~250,000 chars -> ~62,500 tokens > 32,768 limit

    with pytest.raises(AIContextExceededError) as exc_info:
        record_ai_preflight(
            job_id="job-preflight-1",
            provider="cloudflare",
            model="@cf/qwen/qwen3.8-27b",
            operation="script_generation",
            source_text=giant_text,
            attempt=1,
            failover_index=0,
        )

    assert "exceed cloudflare/@cf/qwen/qwen3.8-27b context limit" in str(exc_info.value)


def test_preflight_safe_telemetry_never_contains_source_or_secrets():
    """Preflight telemetry contains character/byte counts and metadata, but never raw source or secrets."""
    secret_source = "Extremely sensitive article content with internal secrets"
    meta = record_ai_preflight(
        job_id="job-preflight-2",
        provider="groq",
        model="groq/compound",
        operation="script_generation",
        source_text=secret_source,
        attempt=1,
        failover_index=0,
    )

    meta_str = str(meta)
    assert secret_source not in meta_str
    assert "Authorization" not in meta_str
    assert "api_key" not in meta_str
    assert meta["source_characters"] == len(secret_source)
    assert meta["provider"] == "groq"
    assert meta["operation"] == "script_generation"


# ==============================================================================
# Article Extraction Cascade & Sanity Metrics Tests
# ==============================================================================

class TestArticleExtractionCascade:
    """Verify the conservative extraction cascade: JSON-LD -> article -> main -> body."""

    def _make_html(self, body_content, json_ld=None, title="Test Page"):
        import json

        parts = [f"<html><head><title>{title}</title></head><body>"]
        if json_ld:
            parts.append(f'<script type="application/ld+json">{json.dumps(json_ld)}</script>')
        parts.append(body_content)
        parts.append("</body></html>")
        return "".join(parts)

    def test_json_ld_extraction(self, monkeypatch):
        """JSON-LD articleBody should be used when available and >200 chars."""
        import socket

        import httpx

        from herald.extraction.url_extractor import extract_article_from_url

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
        )

        long_body = "This is a comprehensive article about technology. " * 10  # >200 chars
        json_ld = {
            "@type": "Article",
            "headline": "Tech Article Title",
            "articleBody": long_body,
        }
        html = self._make_html("<p>Fallback paragraph text here.</p>", json_ld=json_ld)

        transport = httpx.MockTransport(lambda req: httpx.Response(200, text=html, headers={"Content-Type": "text/html"}))
        title, text, url = extract_article_from_url("https://example.com/article", transport=transport)

        assert "Tech Article Title" in title or title == "Tech Article Title"
        assert long_body.strip()[:50] in text

    def test_no_li_elements_in_extraction(self, monkeypatch):
        """<li> elements must NOT be included in extraction to avoid nav/sidebar noise."""
        import socket

        import httpx

        from herald.extraction.url_extractor import extract_article_from_url

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
        )

        html = self._make_html(
            "<article>"
            "<p>Main article content paragraph that is meaningful and long enough to pass thresholds.</p>"
            "<ul><li>Navigation link one</li><li>Navigation link two</li></ul>"
            "<p>Another meaningful paragraph with substance and details that also passes thresholds.</p>"
            "</article>"
        )

        transport = httpx.MockTransport(lambda req: httpx.Response(200, text=html, headers={"Content-Type": "text/html"}))
        title, text, url = extract_article_from_url("https://example.com/article", transport=transport)

        assert "Navigation link" not in text
        assert "Main article content" in text

    def test_boilerplate_detection(self):
        """Boilerplate paragraphs should be filtered."""
        from herald.extraction.url_extractor import _is_boilerplate

        assert _is_boilerplate("We use cookies to improve your experience.") is True
        assert _is_boilerplate("Subscribe to our newsletter") is True
        assert _is_boilerplate("Read more articles") is True
        assert _is_boilerplate("About the author") is True
        assert _is_boilerplate("Share this on Twitter") is True
        assert _is_boilerplate("This is a meaningful paragraph about technology trends.") is False

    def test_structural_boilerplate_removal_without_text_keywords(self, monkeypatch):
        """Structural containers with boilerplate class/id/role/aria should be decomposed, and h4/li ignored."""
        import socket

        import httpx

        from herald.extraction.url_extractor import extract_article_from_url

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
        )

        html = self._make_html(
            "<main>"
            "<p>Primary editorial content that is well written, extensive, and definitely belongs in the podcast script.</p>"
            "<p>Second paragraph detailing the key findings and providing rich substance that passes all threshold limits.</p>"
            '<div class="related-stories">'
            "<p>Completely innocuous text about autumn weather that has no keywords but lives in related block.</p>"
            "</div>"
            '<aside id="author-bio-card">'
            "<p>A profile description with no trigger words that describes someone who wrote articles years ago.</p>"
            "</aside>"
            "<h4>Subheading heading four that should not be collected as paragraph</h4>"
            "<ul><li>List item bullet point that should never be collected</li></ul>"
            "</main>"
        )

        transport = httpx.MockTransport(lambda req: httpx.Response(200, text=html, headers={"Content-Type": "text/html"}))
        title, text, url = extract_article_from_url("https://example.com/article-structural", transport=transport)

        assert "Primary editorial content" in text
        assert "Second paragraph detailing" in text
        assert "autumn weather" not in text, "Structural related-stories content must be removed"
        assert "A profile description" not in text, "Structural author-bio-card content must be removed"
        assert "Subheading heading four" not in text, "<h4> elements must not be collected"
        assert "List item bullet point" not in text, "<li> elements must not be collected"


def test_extraction_sanity_metrics_returned():
    """Verify ExtractionResult contains fetched_bytes, extracted_chars, normalized_chars, paragraph_count."""
    import httpx

    from herald.extraction.url_extractor import extract_article_from_url

    fake_html = """
    <html>
      <head><title>Test Article Title</title></head>
      <body>
        <article>
          <p>This is the first paragraph with enough content to be valid article body.</p>
          <p>This is the second paragraph with detailed explanations of the system design.</p>
          <p>This is the third paragraph concluding the analysis of the project requirements.</p>
        </article>
      </body>
    </html>
    """
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"Content-Type": "text/html"}, text=fake_html)
    )
    with patch("herald.extraction.url_extractor.validate_url_host", return_value=("example.com", 443, "93.184.216.34")):
        res = extract_article_from_url("https://example.com/article", transport=transport)

    assert isinstance(res, tuple)
    assert len(res) == 3
    t, b, u = res
    assert t == "Test Article Title"
    assert "first paragraph" in b
    assert hasattr(res, "metrics")
    assert res.metrics["fetched_bytes"] > 0
    assert res.metrics["extracted_chars"] > 0
    assert res.metrics["paragraph_count"] >= 3
    assert isinstance(res.metrics["pollution_detected"], list)

