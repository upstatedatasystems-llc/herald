"""
Tests for the manual acceptance correction package.

Covers:
- redact_value() list root preservation
- Diagnostics export with list auto_diagnostics_json
- Article extraction cascade (JSON-LD, article, main, body)
- Boilerplate cleanup
- Error classification preservation
- URL context fallback eligibility
- Telegram session recovery + provisional job recovery
- Migration 016 custom_title expansion
- Research normalization truncation handling
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from herald.config import settings
from herald.db.models import JobState, PodcastJob, PodcastTTSChunk, SourceType


# ═══════════════════════════════════════════════════════════════════
# GROUP A: Redaction — redact_value()
# ═══════════════════════════════════════════════════════════════════

class TestRedactValue:
    """Verify redact_value() handles all JSON-serializable types correctly."""

    def test_list_root_preserved(self):
        """auto_diagnostics_json is a list — redact_value must preserve list structure."""
        from herald.services.redaction import redact_value
        records = [
            {"attempt": 1, "stage": "extraction", "error_message": "blocked"},
            {"attempt": 2, "stage": "extraction", "api_key": "sk-secret123"},
        ]
        result = redact_value(records)
        assert isinstance(result, list), "List root must be preserved"
        assert len(result) == 2
        assert result[0]["attempt"] == 1
        assert result[0]["stage"] == "extraction"

    def test_dict_input_delegates_to_redact_dict(self):
        from herald.services.redaction import redact_value, redact_dict
        d = {"api_key": "secret", "stage": "tts"}
        rv = redact_value(d)
        rd = redact_dict(d)
        assert rv == rd

    def test_string_input_delegates_to_redact_text(self):
        from herald.services.redaction import redact_value
        result = redact_value("my api_key=sk-12345 is here")
        assert isinstance(result, str)

    def test_primitive_passthrough(self):
        from herald.services.redaction import redact_value
        assert redact_value(42) == 42
        assert redact_value(3.14) == 3.14
        assert redact_value(True) is True
        assert redact_value(False) is False
        assert redact_value(None) is None

    def test_nested_list_of_dicts(self):
        from herald.services.redaction import redact_value
        data = [
            {"network_probe": {"dns_ok": True, "tcp_ok": True, "summary": "OK"}},
            {"api_key": "secret-key-value"},
        ]
        result = redact_value(data)
        assert isinstance(result, list)
        assert len(result) == 2
        # network_probe should survive
        assert result[0]["network_probe"]["dns_ok"] is True
        # api_key should be redacted
        assert result[1]["api_key"] != "secret-key-value"

    def test_sensitive_keys_redacted_in_list_items(self):
        """Verify secrets inside list items get redacted."""
        from herald.services.redaction import redact_value
        data = [{"password": "hunter2", "stage": "delivery"}]
        result = redact_value(data)
        assert result[0]["password"] != "hunter2"
        assert result[0]["stage"] == "delivery"


# ═══════════════════════════════════════════════════════════════════
# GROUP B: Article Extraction Quality
# ═══════════════════════════════════════════════════════════════════

class TestArticleExtractionCascade:
    """Verify the conservative extraction cascade: JSON-LD → article → main → body."""

    def _make_html(self, body_content, json_ld=None, title="Test Page"):
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

        monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])

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

        monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])

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


# ═══════════════════════════════════════════════════════════════════
# GROUP C: Error Classification Preservation
# ═══════════════════════════════════════════════════════════════════

class TestErrorClassificationPreservation:
    """Verify pipeline preserves specific error categories instead of collapsing to EXTRACTION_FAILURE."""

    def test_dns_error_preserves_category(self, db_session: Session):
        """DNSResolutionError should produce DNS_RESOLUTION_ERROR, not EXTRACTION_FAILURE."""
        from herald.extraction.url_extractor import DNSResolutionError

        dns_err = DNSResolutionError("Could not resolve example.com")

        with (
            patch("herald.core.pipeline.extract_article_from_url", side_effect=dns_err),
            patch("herald.services.failure_diagnostics.collect_failure_diagnostics"),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
            patch("herald.services.performance_metrics.record_stage_metric"),
            patch("herald.services.diagnostic_recorder.record_job_diagnostic_event"),
        ):
            from herald.core.models import HeraldRequest
            from herald.core.pipeline import process_herald_request

            req = HeraldRequest(
                transport="telegram",
                transport_message_id=100,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="literal",
                source_url="https://unresolvable.example.com/article",
            )
            response = process_herald_request(db=db_session, req=req)

        assert response.error_category == "DNS_RESOLUTION_ERROR"
        job = db_session.query(PodcastJob).filter(PodcastJob.id == response.job_id).first()
        assert job is not None
        assert job.error_code == "DNS_RESOLUTION_ERROR"

    def test_source_access_blocked_preserves_category(self, db_session: Session):
        """SourceAccessBlockedError should produce SOURCE_ACCESS_BLOCKED."""
        from herald.extraction.url_extractor import SourceAccessBlockedError

        err = SourceAccessBlockedError("403 Forbidden")

        with (
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.services.failure_diagnostics.collect_failure_diagnostics"),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
            patch("herald.services.performance_metrics.record_stage_metric"),
            patch("herald.services.diagnostic_recorder.record_job_diagnostic_event"),
        ):
            from herald.core.models import HeraldRequest
            from herald.core.pipeline import process_herald_request

            req = HeraldRequest(
                transport="telegram",
                transport_message_id=100,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="literal",
                source_url="https://blocked.example.com/article",
            )
            response = process_herald_request(db=db_session, req=req)

        assert response.error_category == "SOURCE_ACCESS_BLOCKED"
        job = db_session.query(PodcastJob).filter(PodcastJob.id == response.job_id).first()
        assert job is not None
        assert job.error_code == "SOURCE_ACCESS_BLOCKED"


# ═══════════════════════════════════════════════════════════════════
# GROUP D: URL Context Fallback Eligibility
# ═══════════════════════════════════════════════════════════════════

class TestURLContextFallbackEligibility:
    """Verify URL Context fallback scenarios A through K."""

    def test_case_a_direct_403_fallback_succeeds_pipeline_continues(self, db_session: Session):
        """Case A: Direct 403 -> URL Context succeeds -> pipeline continues successfully."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("403 Forbidden", block_reason=BlockReason.PUBLIC_RETRIEVAL_BLOCK)
        mock_script = MagicMock()
        mock_script.model_dump.return_value = {
            "episode_title": "AI Breakthrough",
            "episode_description": "Summary of article",
            "estimated_minutes": 2,
            "segments": [{"segment_title": "Intro", "narration": "Narration text here."}],
        }

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch(
                "herald.gemini.client.extract_article_via_url_context",
                return_value={"title": "Extracted Title", "body": "This is the extracted body text of the article with enough words."},
            ) as mock_url_ctx,
            patch("herald.gemini.client.generate_podcast_script", return_value=mock_script),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=301,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://blocked.example.com/article-403",
                hold_for_approval=False,
            )
            response = process_herald_request(db=db_session, req=req)

        mock_url_ctx.assert_called_once()
        assert response.status == JobState.QUEUED_TTS.value
        job = db_session.query(PodcastJob).filter_by(id=response.job_id).first()
        assert job is not None
        assert job.status == JobState.QUEUED_TTS.value

    def test_case_b_url_context_failure_gives_paste_text_guidance(self, db_session: Session):
        """Case B: URL Context failure -> SOURCE_ACCESS_BLOCKED + paste-text guidance."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("403 Forbidden", block_reason=BlockReason.PUBLIC_RETRIEVAL_BLOCK)

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.gemini.client.extract_article_via_url_context", return_value=None) as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=302,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://blocked.example.com/article-failed-ctx",
            )
            response = process_herald_request(db=db_session, req=req)

        mock_url_ctx.assert_called_once()
        assert response.status == JobState.FAILED_FINAL.value
        assert response.error_category == "SOURCE_ACCESS_BLOCKED"
        assert "could not retrieve the original public page" in response.message
        assert "paste the article text directly" in response.message

    def test_case_c_literal_mode_zero_url_context_calls(self, db_session: Session):
        """Case C: Literal -> URL Context call count zero + actionable message."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("403 Forbidden", block_reason=BlockReason.PUBLIC_RETRIEVAL_BLOCK)

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=303,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="literal",
                source_url="https://blocked.example.com/article-literal",
            )
            response = process_herald_request(db=db_session, req=req)

        assert mock_url_ctx.call_count == 0
        assert response.status == JobState.FAILED_FINAL.value
        assert "Literal mode does not use AI-assisted URL retrieval" in response.message
        assert "paste the article text directly" in response.message

    def test_case_d_ssrf_zero_url_context_calls(self, db_session: Session):
        """Case D: SSRF -> URL Context call count zero."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import SSRFVulnerabilityError

        err = SSRFVulnerabilityError("Security violation: internal IP")

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=304,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="http://169.254.169.254/latest/meta-data",
            )
            response = process_herald_request(db=db_session, req=req)

        assert mock_url_ctx.call_count == 0
        assert response.status == JobState.FAILED_FINAL.value
        assert response.error_category == "SSRF_PROTECTION"

    def test_case_e_401_auth_required_zero_url_context_calls(self, db_session: Session):
        """Case E: 401 auth required -> URL Context call count zero."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("HTTP 401 Unauthorized", block_reason=BlockReason.AUTH_REQUIRED)

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=305,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://secret.example.com/article",
            )
            response = process_herald_request(db=db_session, req=req)

        assert mock_url_ctx.call_count == 0
        assert response.status == JobState.FAILED_FINAL.value

    def test_case_f_paywall_zero_url_context_calls(self, db_session: Session):
        """Case F: Paywall marker -> URL Context call count zero."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("Paywall detected", block_reason=BlockReason.PAYWALL)

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=306,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://paywall.example.com/article",
            )
            response = process_herald_request(db=db_session, req=req)

        assert mock_url_ctx.call_count == 0
        assert response.status == JobState.FAILED_FINAL.value

    def test_case_g_captcha_zero_url_context_calls(self, db_session: Session):
        """Case G: CAPTCHA marker -> URL Context call count zero."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("Captcha challenge", block_reason=BlockReason.CAPTCHA)

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=307,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://captcha.example.com/article",
            )
            response = process_herald_request(db=db_session, req=req)

        assert mock_url_ctx.call_count == 0
        assert response.status == JobState.FAILED_FINAL.value

    def test_case_h_direct_extraction_success_zero_url_context_calls(self, db_session: Session):
        """Case H: Direct extraction success -> URL Context call count zero."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request

        mock_script = MagicMock()
        mock_script.model_dump.return_value = {
            "episode_title": "Direct Extraction",
            "episode_description": "Summary",
            "estimated_minutes": 2,
            "segments": [{"segment_title": "Intro", "narration": "Direct narration text."}],
        }

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch(
                "herald.core.pipeline.extract_article_from_url",
                return_value=("Direct Title", "Direct body text with plenty of content.", "https://example.com/direct"),
            ),
            patch("herald.gemini.client.extract_article_via_url_context") as mock_url_ctx,
            patch("herald.gemini.client.generate_podcast_script", return_value=mock_script),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=308,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://example.com/direct",
                hold_for_approval=False,
            )
            response = process_herald_request(db=db_session, req=req)

        assert mock_url_ctx.call_count == 0
        assert response.status == JobState.QUEUED_TTS.value

    def test_case_i_direct_http_telemetry_persisted(self, db_session: Session):
        """Case I: DIRECT_HTTP telemetry persisted in DB metrics and diagnostic events."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.db.models import JobDiagnosticEvent, JobProcessingMetric

        mock_script = MagicMock()
        mock_script.model_dump.return_value = {
            "episode_title": "Telemetry Direct",
            "episode_description": "Summary",
            "estimated_minutes": 1,
            "segments": [{"segment_title": "Intro", "narration": "Direct narration."}],
        }

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch(
                "herald.core.pipeline.extract_article_from_url",
                return_value=("Direct Title", "Direct body text for telemetry verification.", "https://example.com/telemetry-direct"),
            ),
            patch("herald.gemini.client.generate_podcast_script", return_value=mock_script),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=309,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://example.com/telemetry-direct",
                hold_for_approval=False,
            )
            response = process_herald_request(db=db_session, req=req)

        metric = (
            db_session.query(JobProcessingMetric)
            .filter_by(job_id=response.job_id, stage="URL_EXTRACTION")
            .first()
        )
        assert metric is not None
        assert metric.metadata_json.get("extraction_method") == "DIRECT_HTTP"

        event = (
            db_session.query(JobDiagnosticEvent)
            .filter_by(job_id=response.job_id, event_type="EXTRACTION_SUCCESS")
            .first()
        )
        assert event is not None
        assert event.metadata_json_sanitized.get("extraction_method") == "DIRECT_HTTP"

    def test_case_j_gemini_url_context_telemetry_persisted(self, db_session: Session):
        """Case J: GEMINI_URL_CONTEXT telemetry persisted in DB metrics and diagnostic events."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.db.models import JobDiagnosticEvent, JobProcessingMetric
        from herald.extraction.url_extractor import BlockReason, SourceAccessBlockedError

        err = SourceAccessBlockedError("403 Forbidden", block_reason=BlockReason.PUBLIC_RETRIEVAL_BLOCK)
        mock_script = MagicMock()
        mock_script.model_dump.return_value = {
            "episode_title": "Telemetry Fallback",
            "episode_description": "Summary",
            "estimated_minutes": 1,
            "segments": [{"segment_title": "Intro", "narration": "Fallback narration."}],
        }

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch.object(settings, "AI_PROVIDER", "gemini"),
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch(
                "herald.gemini.client.extract_article_via_url_context",
                return_value={"title": "Fallback Title", "body": "Fallback extracted article body content with sufficient length."},
            ),
            patch("herald.gemini.client.generate_podcast_script", return_value=mock_script),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            req = HeraldRequest(
                transport="telegram",
                transport_message_id=310,
                requester_identity="telegram:12345",
                delivery_target="12345",
                request_mode="standard",
                source_url="https://example.com/telemetry-fallback",
                hold_for_approval=False,
            )
            response = process_herald_request(db=db_session, req=req)

        metric = (
            db_session.query(JobProcessingMetric)
            .filter_by(job_id=response.job_id, stage="URL_EXTRACTION")
            .first()
        )
        assert metric is not None
        assert metric.metadata_json.get("extraction_method") == "GEMINI_URL_CONTEXT"
        assert metric.metadata_json.get("fallback_attempted") is True
        assert metric.metadata_json.get("fallback_result") == "SUCCESS"

        event = (
            db_session.query(JobDiagnosticEvent)
            .filter_by(job_id=response.job_id, event_type="EXTRACTION_SUCCESS")
            .first()
        )
        assert event is not None
        assert event.metadata_json_sanitized.get("extraction_method") == "GEMINI_URL_CONTEXT"
        assert event.metadata_json_sanitized.get("fallback_attempted") is True
        assert event.metadata_json_sanitized.get("fallback_result") == "SUCCESS"

    def test_case_k_url_context_ai_interaction_persisted(self, db_session: Session):
        """Case K: url_context_extraction AI interaction persisted via extract_article_via_url_context."""
        from herald.db.models import AIInteraction
        from herald.gemini.client import extract_article_via_url_context

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"x-goog-request-id": "req-ctx-123"}
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps({
                                    "title": "Valid Title",
                                    "body": "This is a valid extracted body that is well over one hundred characters long to ensure validation succeeds.",
                                })
                            }
                        ]
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 50,
                "candidatesTokenCount": 30,
                "totalTokenCount": 80,
            },
        }

        test_job_id = str(uuid.uuid4())
        job = PodcastJob(
            id=test_job_id,
            transport="api",
            source_hash="hash-ctx-ai",
            source_text="test",
            request_mode="standard",
            source_type="url",
            status=JobState.EXTRACTING.value,
        )
        db_session.add(job)
        db_session.commit()

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch("httpx.Client.post", return_value=mock_resp),
        ):
            result = extract_article_via_url_context(
                url="https://example.com/test-ai-interaction",
                job_id=test_job_id,
            )

        assert result is not None
        assert result["title"] == "Valid Title"

        interaction = (
            db_session.query(AIInteraction)
            .filter_by(job_id=test_job_id, operation="url_context_extraction")
            .first()
        )
        assert interaction is not None
        assert interaction.success is True
        assert interaction.metadata_json.get("finish_reason") == "STOP"
        assert interaction.metadata_json.get("requested_max_output_tokens") == settings.GEMINI_MAX_OUTPUT_TOKENS

    def test_url_context_validation_failure_records_failure_telemetry(self, db_session: Session):
        """URL Context returning invalid/empty/short body records success=False and returns None."""
        from herald.db.models import AIInteraction
        from herald.gemini.client import extract_article_via_url_context

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"x-goog-request-id": "req-ctx-fail-456"}
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps({
                                    "title": "Short Page",
                                    "body": "Too short body text.",
                                })
                            }
                        ]
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 50,
                "candidatesTokenCount": 10,
                "totalTokenCount": 60,
            },
        }

        test_job_id = str(uuid.uuid4())
        job = PodcastJob(
            id=test_job_id,
            transport="api",
            source_hash="hash-ctx-fail",
            source_text="test",
            request_mode="standard",
            source_type="url",
            status=JobState.EXTRACTING.value,
        )
        db_session.add(job)
        db_session.commit()

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-api-key"),
            patch("httpx.Client.post", return_value=mock_resp),
        ):
            result = extract_article_via_url_context(
                url="https://example.com/test-ai-fail",
                job_id=test_job_id,
            )

        assert result is None

        interaction = (
            db_session.query(AIInteraction)
            .filter_by(job_id=test_job_id, operation="url_context_extraction")
            .first()
        )
        assert interaction is not None
        assert interaction.success is False
        assert "minimum 100 required" in (interaction.error_message or "")



# ═══════════════════════════════════════════════════════════════════
# GROUP E: Telegram Session Recovery
# ═══════════════════════════════════════════════════════════════════

class TestTelegramSessionRecovery:
    """Verify session rollback and provisional job recovery."""

    def test_exception_triggers_rollback(self, db_session: Session):
        """Exception during process_herald_request must trigger db.rollback()."""
        from herald.telegram.bot import handle_telegram_content_message

        client = MagicMock()
        message = {
            "chat": {"id": 99999, "type": "private"},
            "from": {"id": 88888},
            "message_id": 500,
            "text": "https://example.com/article",
        }

        with (
            patch("herald.telegram.bot.is_user_authorized", return_value=True),
            patch("herald.telegram.bot.has_owner", return_value=True),
            patch(
                "herald.telegram.bot.process_herald_request",
                side_effect=RuntimeError("Simulated crash"),
            ),
            patch("herald.telegram.bot.get_effective_user_preferences", return_value={}),
            patch.object(db_session, "rollback") as mock_rollback,
        ):
            handle_telegram_content_message(db_session, client, message)

        mock_rollback.assert_called_once()
        # Verify error message was sent
        client.send_message.assert_called()
        call_args = client.send_message.call_args
        assert "could not process" in call_args.kwargs.get("text", call_args[1].get("text", "")).lower() or \
               "could not process" in str(call_args)


# ═══════════════════════════════════════════════════════════════════
# GROUP F: Migration 016 — Long Titles
# ═══════════════════════════════════════════════════════════════════

class TestMigration016:
    """Verify migration 016 expand custom_title to Text."""

    def test_migration_file_exists(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        path = repo_root / "migrations" / "versions" / "016_expand_custom_title_text.py"
        assert path.exists(), "Migration 016 file must exist"

    def test_migration_revision_chain(self):
        import importlib.util
        repo_root = Path(__file__).resolve().parent.parent.parent
        mig_path = repo_root / "migrations" / "versions" / "016_expand_custom_title_text.py"
        spec = importlib.util.spec_from_file_location(
            "migration_016",
            mig_path,
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "016_expand_custom_title_text"
        assert mod.down_revision == "015_rerun_lineage_diagnostics"

    def test_long_title_persists(self, db_session: Session):
        """Titles >255 characters should persist without truncation after migration."""
        long_title = "A" * 500
        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="api",
            source_hash="hash-long-title",
            source_text="test source",
            request_mode="standard",
            source_type="text",
            custom_title=long_title,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)
        assert len(job.custom_title) == 500
        assert job.custom_title == long_title


# ═══════════════════════════════════════════════════════════════════
# GROUP G: Research Normalization
# ═══════════════════════════════════════════════════════════════════

class TestResearchNormalizationConfig:
    """Verify GEMINI_RESEARCH_NORMALIZATION_INITIAL_OUTPUT_TOKENS and MAX config exists."""

    def test_config_field_exists(self):
        assert hasattr(settings, "GEMINI_RESEARCH_NORMALIZATION_INITIAL_OUTPUT_TOKENS")
        assert settings.GEMINI_RESEARCH_NORMALIZATION_INITIAL_OUTPUT_TOKENS == 8192
        assert hasattr(settings, "GEMINI_RESEARCH_NORMALIZATION_MAX_OUTPUT_TOKENS")
        assert settings.GEMINI_RESEARCH_NORMALIZATION_MAX_OUTPUT_TOKENS == 16384

    def test_config_field_is_int(self):
        assert isinstance(settings.GEMINI_RESEARCH_NORMALIZATION_INITIAL_OUTPUT_TOKENS, int)
        assert isinstance(settings.GEMINI_RESEARCH_NORMALIZATION_MAX_OUTPUT_TOKENS, int)


# ═══════════════════════════════════════════════════════════════════
# GROUP H: Stale EXTRACTING Recovery Narrowing
# ═══════════════════════════════════════════════════════════════════

class TestStaleExtractingRecoveryNarrowing:
    """Verify stale EXTRACTING recovery for Telegram intake jobs."""

    def test_api_extracting_job_not_recovered(self, db_session: Session):
        """API-transport EXTRACTING jobs should NOT be recovered by stale recovery."""
        from apps.api.main import ops_stale_recovery

        stale_time = datetime.now(UTC) - timedelta(minutes=30)
        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="api",
            source_hash="hash-api-stale",
            source_text="test source",
            request_mode="standard",
            source_type="text",
            status=JobState.EXTRACTING.value,
            claimed_at=stale_time,
            last_heartbeat_at=stale_time,
        )
        db_session.add(job)
        db_session.commit()

        with patch("apps.api.main.ensure_terminal_diagnostics_archive"):
            result = ops_stale_recovery(db=db_session)

        db_session.refresh(job)
        assert job.status == JobState.EXTRACTING.value, "API EXTRACTING job should not be recovered"
        assert result["recovered_jobs"] == 0

    def test_stale_telegram_no_heartbeat_recovered_to_failed_final(self, db_session: Session):
        """Stale Telegram EXTRACTING job with NO claim and NO heartbeat must become FAILED_FINAL."""
        from apps.api.main import ops_stale_recovery

        stale_time = datetime.now(UTC) - timedelta(minutes=30)
        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="telegram",
            telegram_chat_id=12345,
            telegram_message_id=999,
            source_hash="hash-tg-no-heartbeat",
            source_text="",
            request_mode="standard",
            source_type="url",
            status=JobState.EXTRACTING.value,
            created_at=stale_time,
            updated_at=stale_time,
            claimed_at=None,
            last_heartbeat_at=None,
        )
        db_session.add(job)
        db_session.commit()

        with patch("apps.api.main.ensure_terminal_diagnostics_archive") as mock_archive:
            result = ops_stale_recovery(db=db_session)

        db_session.refresh(job)
        assert job.status == JobState.FAILED_FINAL.value, "Stale no-heartbeat Telegram job must transition to FAILED_FINAL"
        assert job.error_code == "INTAKE_TIMEOUT"
        assert job.failed_stage == "EXTRACTING"
        assert result["recovered_jobs"] >= 1
        mock_archive.assert_called_with(job.id, JobState.FAILED_FINAL.value)

    def test_recent_telegram_no_heartbeat_unchanged(self, db_session: Session):
        """Recent Telegram EXTRACTING job with no claim/heartbeat should remain untouched."""
        from apps.api.main import ops_stale_recovery

        recent_time = datetime.now(UTC) - timedelta(minutes=3)
        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="telegram",
            telegram_chat_id=12345,
            telegram_message_id=998,
            source_hash="hash-tg-recent",
            source_text="",
            request_mode="standard",
            source_type="url",
            status=JobState.EXTRACTING.value,
            created_at=recent_time,
            updated_at=recent_time,
            claimed_at=None,
            last_heartbeat_at=None,
        )
        db_session.add(job)
        db_session.commit()

        with patch("apps.api.main.ensure_terminal_diagnostics_archive"):
            result = ops_stale_recovery(db=db_session)

        db_session.refresh(job)
        assert job.status == JobState.EXTRACTING.value, "Recent Telegram job must not be recovered"

    def test_row_advances_before_recovery_not_overwritten(self, db_session: Session):
        """If a row advances to another status, stale recovery skips it without overwriting."""
        from apps.api.main import ops_stale_recovery

        stale_time = datetime.now(UTC) - timedelta(minutes=30)
        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="telegram",
            telegram_chat_id=12345,
            telegram_message_id=997,
            source_hash="hash-tg-advanced",
            source_text="complete source",
            request_mode="standard",
            source_type="url",
            status=JobState.COMPLETE.value,
            created_at=stale_time,
            updated_at=stale_time,
            claimed_at=None,
            last_heartbeat_at=None,
        )
        db_session.add(job)
        db_session.commit()

        with patch("apps.api.main.ensure_terminal_diagnostics_archive") as mock_archive:
            result = ops_stale_recovery(db=db_session)

        db_session.refresh(job)
        assert job.status == JobState.COMPLETE.value
        mock_archive.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
# GROUP I: Failure-Diagnostics Export In ZIP
# ═══════════════════════════════════════════════════════════════════

class TestFailureDiagnosticsZipExport:
    """Verify failure-diagnostics.json in diagnostics ZIP retains list root and redacts secrets."""

    def test_zip_contains_list_root_failure_diagnostics(self, db_session: Session, tmp_path: Path, monkeypatch):
        import zipfile
        from herald.services.diagnostics_export import generate_job_diagnostics_zip

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("herald.config.settings.HERALD_LOG_DIR", str(log_dir))

        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="telegram",
            request_mode="standard",
            source_hash="hash-diag-test",
            source_text="Test source text for diagnostics export.",
            status=JobState.FAILED_FINAL.value,
            error_code="SOURCE_ACCESS_BLOCKED",
            error_detail="Cloudflare 403 Forbidden",
            auto_diagnostics_json=[
                {
                    "attempt": 1,
                    "stage": "extraction",
                    "error_category": "SOURCE_ACCESS_BLOCKED",
                    "network_probe": {"dns_ok": True, "tcp_ok": True, "tls_ok": True},
                    "api_key": "sk-secret-do-not-leak",
                },
                {
                    "attempt": 2,
                    "stage": "extraction",
                    "error_category": "SOURCE_ACCESS_BLOCKED",
                    "error_message": "Cloudflare captcha challenged",
                },
            ],
        )
        db_session.add(job)
        db_session.commit()

        zip_path = generate_job_diagnostics_zip(db_session, job)
        assert zip_path is not None and Path(zip_path).exists()

        with zipfile.ZipFile(zip_path, "r") as z:
            assert "failure-diagnostics.json" in z.namelist()
            content_str = z.read("failure-diagnostics.json").decode("utf-8")
            content = json.loads(content_str)

        # Must be a list, NOT an empty dict {}
        assert isinstance(content, list), f"Expected list root, got {type(content)}: {content_str}"
        assert len(content) == 2
        assert content[0]["attempt"] == 1
        assert content[0]["network_probe"]["dns_ok"] is True
        # Secret must be redacted
        assert content[0]["api_key"] != "sk-secret-do-not-leak"

    def test_legacy_diagnostics_zip_repair(self, db_session: Session, tmp_path: Path, monkeypatch):
        """Pre-existing ZIP with legacy failure-diagnostics.json: {} is atomically repaired."""
        import zipfile
        from herald.services.diagnostics_export import ensure_terminal_diagnostics_archive, get_terminal_diagnostics_path

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("herald.config.settings.HERALD_LOG_DIR", str(log_dir))

        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="telegram",
            request_mode="standard",
            source_hash="hash-diag-repair",
            source_text="Test source text for repair.",
            status=JobState.FAILED_FINAL.value,
            error_code="SOURCE_ACCESS_BLOCKED",
            error_detail="Cloudflare 403 Forbidden",
            auto_diagnostics_json=[
                {
                    "attempt": 1,
                    "stage": "extraction",
                    "error_category": "SOURCE_ACCESS_BLOCKED",
                    "api_key": "sk-secret-do-not-leak",
                }
            ],
        )
        db_session.add(job)
        db_session.commit()

        # Write a dummy legacy ZIP containing {} for failure-diagnostics.json
        archive_path = get_terminal_diagnostics_path(job.id, job.status)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path, "w") as z:
            z.writestr("failure-diagnostics.json", "{}\n")
            z.writestr("job.json", "{}\n")

        # Verify it initially contains {}
        with zipfile.ZipFile(archive_path, "r") as z:
            initial_content = json.loads(z.read("failure-diagnostics.json").decode("utf-8"))
            assert isinstance(initial_content, dict)

        # Call ensure_terminal_diagnostics_archive
        repaired_path = ensure_terminal_diagnostics_archive(job.id, JobState.FAILED_FINAL.value)
        assert repaired_path is not None and Path(repaired_path).exists()

        # Verify it was regenerated with the valid list root and redacted secret
        with zipfile.ZipFile(repaired_path, "r") as z:
            repaired_content = json.loads(z.read("failure-diagnostics.json").decode("utf-8"))
            assert isinstance(repaired_content, list), "Repaired ZIP must have list root"
            assert len(repaired_content) == 1
            assert repaired_content[0]["attempt"] == 1
            assert repaired_content[0]["api_key"] != "sk-secret-do-not-leak"


# ═══════════════════════════════════════════════════════════════════
# GROUP J: Migration 016 Downgrade Safety Guard
# ═══════════════════════════════════════════════════════════════════

class TestMigration016DowngradeGuard:
    """Verify migration 016 downgrade safely rejects titles >255 chars."""

    def test_downgrade_fails_when_title_exceeds_255(self, monkeypatch):
        import importlib.util

        mock_op = MagicMock()
        mock_bind = MagicMock()
        mock_op.get_bind.return_value = mock_bind

        # First query: MAX(LENGTH(custom_title)) returns 300
        mock_max_res = MagicMock()
        mock_max_res.scalar.return_value = 300
        # Second query: COUNT(*) returns 2
        mock_count_res = MagicMock()
        mock_count_res.scalar.return_value = 2

        mock_bind.execute.side_effect = [mock_max_res, mock_count_res]

        p = Path("migrations/versions/016_expand_custom_title_text.py").resolve()
        spec = importlib.util.spec_from_file_location("mig_016", p)
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        monkeypatch.setattr(mig, "op", mock_op)

        with pytest.raises(Exception, match="Cannot downgrade: 2 custom_title values exceed 255 characters"):
            mig.downgrade()

    def test_downgrade_succeeds_when_all_titles_fit(self, monkeypatch):
        import importlib.util

        mock_op = MagicMock()
        mock_bind = MagicMock()
        mock_op.get_bind.return_value = mock_bind

        mock_max_res = MagicMock()
        mock_max_res.scalar.return_value = 200
        mock_bind.execute.return_value = mock_max_res

        p = Path("migrations/versions/016_expand_custom_title_text.py").resolve()
        spec = importlib.util.spec_from_file_location("mig_016", p)
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        monkeypatch.setattr(mig, "op", mock_op)

        mig.downgrade()
        mock_op.batch_alter_table.assert_called_once_with("podcast_jobs")


# ═══════════════════════════════════════════════════════════════════
# GROUP K: Research Normalization 40-Source & Truncation Handling
# ═══════════════════════════════════════════════════════════════════

class TestResearchNormalization40Sources:
    """Verify research normalization with large source registries."""

    def _generate_mock_sources(self, count: int = 40):
        return [
            {
                "source_id": f"S{i}",
                "title": f"Authoritative Study {i}: Advances in Research",
                "url": f"https://doi.org/10.1000/study-{i}",
                "domain": "doi.org",
                "retrieved_at": "2026-09-09T12:00:00Z",
                "search_query": f"research topic {i}",
            }
            for i in range(1, count + 1)
        ]

    def test_schema_excludes_research_sources_and_injects_locally(self):
        """Gemini schema must NOT include research_sources, and sources are injected locally."""
        from herald.gemini.client import normalize_research_dossier

        sources = self._generate_mock_sources(40)
        captured_payload = {}

        def mock_post(url, json=None, headers=None):
            nonlocal captured_payload
            captured_payload = json
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            valid_dossier = {
                "source_summary": "Comprehensive summary of findings.",
                "verification": [
                    {
                        "source_claim": "Claim 1",
                        "status": "supported",
                        "notes": "Verified against studies",
                        "source_ids": ["S1", "S2"],
                    }
                ],
                "useful_context": [
                    {
                        "fact": "Fact 1",
                        "why_it_matters": "Context is essential",
                        "source_ids": ["S3"],
                    }
                ],
                "outdated_or_uncertain": [],
            }
            mock_resp.json.return_value = {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": json_mod.dumps(valid_dossier)}]},
                    }
                ],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 500, "totalTokenCount": 1500},
            }
            return mock_resp

        import json as json_mod

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-key"),
            patch("httpx.Client.post", side_effect=mock_post),
            patch("herald.gemini.client._record_gemini_interaction"),
        ):
            dossier = normalize_research_dossier(
                source_text="Test primary source.",
                grounded_research_data={
                    "raw_text": "Grounded research notes.",
                    "research_sources": sources,
                },
            )

        # 1. Schema must NOT have research_sources
        gen_cfg = captured_payload.get("generationConfig", {})
        schema_props = gen_cfg.get("responseSchema", {}).get("properties", {})
        assert "research_sources" not in schema_props, "research_sources must not be in Gemini schema"

        # 2. Returned dossier must have all 40 sources locally injected
        assert len(dossier.research_sources) == 40
        assert dossier.research_sources[0].source_id == "S1"
        assert dossier.research_sources[39].source_id == "S40"

    def test_finish_reason_max_tokens_triggers_truncation_error(self):
        """When finishReason is MAX_TOKENS, GeminiOutputTruncatedError must be raised without attempting JSON parse."""
        from herald.gemini.client import GeminiOutputTruncatedError, normalize_research_dossier

        def mock_post_truncated(url, json=None, headers=None):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "candidates": [
                    {
                        "finishReason": "MAX_TOKENS",
                        "content": {"parts": [{"text": '{"source_summary": "Incomplete json'}]},
                    }
                ],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 8192, "totalTokenCount": 9192},
            }
            return mock_resp

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-key"),
            patch.object(settings, "GEMINI_RETRY_COUNT", 1),
            patch("httpx.Client.post", side_effect=mock_post_truncated),
            patch("herald.gemini.client._record_gemini_interaction"),
        ):
            with pytest.raises(GeminiOutputTruncatedError) as exc_info:
                normalize_research_dossier(
                    source_text="Test source.",
                    grounded_research_data={
                        "raw_text": "Evidence.",
                        "research_sources": [{"source_id": "S1"}],
                    },
                )

        assert "output truncated" in str(exc_info.value).lower()

    def test_research_normalization_records_stop_telemetry(self):
        """Successful normalization records finish_reason='STOP' and requested_max_output_tokens=8192."""
        from herald.gemini.client import normalize_research_dossier

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"x-goog-request-id": "req-norm-stop-123"}
        valid_dossier = {
            "source_summary": "Summary of research findings.",
            "verification": [],
            "useful_context": [],
            "outdated_or_uncertain": [],
        }
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps(valid_dossier)}]},
                }
            ],
            "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 450, "totalTokenCount": 1650},
        }

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-key"),
            patch("httpx.Client.post", return_value=mock_resp),
            patch("herald.gemini.client._record_gemini_interaction") as mock_record,
        ):
            dossier = normalize_research_dossier(
                source_text="Test source.",
                grounded_research_data={
                    "raw_text": "Evidence notes.",
                    "research_sources": self._generate_mock_sources(1),
                },
                job_id="job-norm-stop",
            )

        assert dossier is not None
        mock_record.assert_called_once()
        kwargs = mock_record.call_args.kwargs
        assert kwargs["success"] is True
        assert kwargs["finish_reason"] == "STOP"
        assert kwargs["requested_max_output_tokens"] == 8192
        assert kwargs["job_id"] == "job-norm-stop"

    def test_research_normalization_records_max_tokens_and_doubles_budget(self):
        """MAX_TOKENS records finish_reason='MAX_TOKENS' and doubles budget on retry."""
        from herald.gemini.client import normalize_research_dossier

        sent_payloads = []

        def mock_post_retry(url, json=None, headers=None):
            sent_payloads.append(json)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            if len(sent_payloads) == 1:
                # First attempt truncated
                mock_resp.json.return_value = {
                    "candidates": [
                        {
                            "finishReason": "MAX_TOKENS",
                            "content": {"parts": [{"text": '{"source_summary": "Incomplete'}]},
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 8192, "totalTokenCount": 9192},
                }
            else:
                # Second attempt succeeds
                valid_dossier = {
                    "source_summary": "Full summary on retry.",
                    "verification": [],
                    "useful_context": [],
                    "outdated_or_uncertain": [],
                }
                mock_resp.json.return_value = {
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {"parts": [{"text": json_mod.dumps(valid_dossier)}]},
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 12000, "totalTokenCount": 13000},
                }
            return mock_resp

        import json as json_mod

        with (
            patch.object(settings, "GEMINI_API_KEY", "test-key"),
            patch.object(settings, "GEMINI_RETRY_COUNT", 2),
            patch("httpx.Client.post", side_effect=mock_post_retry),
            patch("herald.gemini.client._record_gemini_interaction") as mock_record,
            patch("time.sleep"),
        ):
            dossier = normalize_research_dossier(
                source_text="Test source.",
                grounded_research_data={
                    "raw_text": "Evidence notes.",
                    "research_sources": self._generate_mock_sources(1),
                },
                job_id="job-norm-retry",
            )

        assert dossier is not None
        assert len(sent_payloads) == 2
        # First request had 8192
        assert sent_payloads[0]["generationConfig"]["maxOutputTokens"] == 8192
        # Second request doubled to 16384 (hard cap)
        assert sent_payloads[1]["generationConfig"]["maxOutputTokens"] == 16384

        assert mock_record.call_count == 2
        call1_kwargs = mock_record.call_args_list[0].kwargs
        assert call1_kwargs["success"] is False
        assert call1_kwargs["finish_reason"] == "MAX_TOKENS"
        assert call1_kwargs["requested_max_output_tokens"] == 8192

        call2_kwargs = mock_record.call_args_list[1].kwargs
        assert call2_kwargs["success"] is True
        assert call2_kwargs["finish_reason"] == "STOP"
        assert call2_kwargs["requested_max_output_tokens"] == 16384


# ═══════════════════════════════════════════════════════════════════
# GROUP L: Provisional Job Intake Crash Recovery
# ═══════════════════════════════════════════════════════════════════

class TestProvisionalJobIntakeRecovery:
    """Verify provisional EXTRACTING job is recovered to FAILED_FINAL on unexpected intake crashes."""

    def test_provisional_job_recovered_on_crash(self, db_session: Session):
        from herald.telegram.bot import handle_telegram_content_message

        # Create provisional job in db
        chat_id = 77777
        msg_id = 888
        provisional = PodcastJob(
            id=str(uuid.uuid4()),
            transport="telegram",
            telegram_chat_id=chat_id,
            telegram_message_id=msg_id,
            request_mode="standard",
            source_type="url",
            source_hash="hash-prov-crash",
            source_text="provisional text",
            status=JobState.EXTRACTING.value,
        )
        db_session.add(provisional)
        db_session.commit()

        client = MagicMock()
        message = {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 12345},
            "message_id": msg_id,
            "text": "https://example.com/article",
        }

        with (
            patch("herald.telegram.bot.is_user_authorized", return_value=True),
            patch("herald.telegram.bot.has_owner", return_value=True),
            patch(
                "herald.telegram.bot.process_herald_request",
                side_effect=RuntimeError("Intake crash during extraction"),
            ),
            patch("herald.telegram.bot.get_effective_user_preferences", return_value={}),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            handle_telegram_content_message(db_session, client, message)

        db_session.refresh(provisional)
        assert provisional.status == JobState.FAILED_FINAL.value
        assert provisional.error_code == "INTAKE_CRASH"
        assert provisional.failed_stage == "EXTRACTION"

        from herald.db.models import JobDiagnosticEvent
        event = (
            db_session.query(JobDiagnosticEvent)
            .filter_by(job_id=provisional.id, event_type="UNEXPECTED_INTAKE_FAILURE")
            .first()
        )
        assert event is not None
        assert event.metadata_json_sanitized.get("prior_state") == "EXTRACTING"
        assert event.metadata_json_sanitized.get("failure_stage") == "EXTRACTION"
        assert event.metadata_json_sanitized.get("error_category") == "INTAKE_CRASH"


# ═══════════════════════════════════════════════════════════════════
# GROUP M: Long Title End-to-End Boundary (>255 chars)
# ═══════════════════════════════════════════════════════════════════

class TestLongTitleEndToEndBoundary:
    """Verify >255 character titles persist canonically without truncation, and formatters/slugs remain bounded."""

    def test_long_title_end_to_end_boundary(self, db_session: Session):
        from herald.services.diagnostics_export import _sanitize_slug
        from herald.services.drive_service import build_user_facing_drive_filename, sanitize_filename_title
        from herald.telegram.formatters import format_approval, format_completion, format_queued

        title_300 = "Comprehensive Deep Dive Into Advanced Neural Architectures and Transformer Optimizations in Production Systems " * 3
        assert len(title_300) > 300

        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="telegram",
            source_hash="hash-boundary-long-title",
            source_text="Full text source content for boundary verification.",
            request_mode="standard",
            source_type="text",
            custom_title=title_300,
            script_json={
                "episode_title": title_300,
                "episode_description": "Detailed multi-part podcast episode discussing modern deep learning.",
                "estimated_minutes": 10,
                "segments": [{"segment_title": "Intro", "narration": "Welcome to our discussion."}],
            },
            status=JobState.COMPLETE.value,
            audio_duration_seconds=600.0,
            local_audio_path="/tmp/test_boundary_audio.mp3",
            created_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # 1. Complete canonical title persists in DB without truncation
        assert job.custom_title == title_300
        assert len(job.custom_title) > 255

        # 2. Telegram formatters remain bounded
        caption = format_completion(job, actual_chunks_count=5, file_size_bytes=8_000_000)
        assert len(caption) <= 1024, f"Telegram audio caption must be <= 1024 characters, got {len(caption)}"
        assert "..." in caption, "Long title should be safely truncated in presentation"

        queued_text = format_queued(job, script_json=job.script_json)
        assert len(queued_text) <= 4096, "Queued card text must fit in Telegram message limit"

        approval_text, markup = format_approval(job, script_json=job.script_json)
        assert len(approval_text) <= 4096, "Approval card text must fit in Telegram message limit"

        # 3. User-facing drive filename is sanitized and bounded (title <= 120 chars)
        sanitized_title = sanitize_filename_title(title_300)
        assert len(sanitized_title) <= 120
        filename = build_user_facing_drive_filename(
            title=title_300,
            created_at=job.created_at,
            mode="Standard",
            extension="mp3",
        )
        assert filename.startswith(sanitized_title)
        assert filename.endswith(".mp3")
        assert len(filename) < 200

        # 4. Diagnostic filename / slug remains safe and bounded <= 32 chars
        slug = _sanitize_slug(job.custom_title)
        assert len(slug) <= 32
        assert not any(c in slug for c in r'<>:"/\|?*')


