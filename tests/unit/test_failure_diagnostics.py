"""
Unit test suite for stage-aware failure diagnostics & anti-SSRF protections.
"""

import json
import socket
import ssl
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import Base, JobState, PodcastJob
from herald.extraction.url_extractor import (
    DNSResolutionError,
    SourceAccessBlockedError,
    SSRFVulnerabilityError,
)
from herald.gemini.client import GeminiModelUnavailableError
from herald.services.failure_diagnostics import (
    collect_failure_diagnostics,
    format_concise_failure_summary,
)
from herald.telegram.formatters import format_generation_failure_card


@pytest.fixture
def in_memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_diagnostics_dns_success_tcp_failure():
    """Verify DNS resolution succeeds but TCP failure is captured accurately."""
    mock_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

    with patch("socket.getaddrinfo", return_value=mock_addrinfo), \
         patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = ConnectionRefusedError("Connection refused")
        mock_sock_cls.return_value = mock_sock

        res = collect_failure_diagnostics(
            stage="extraction",
            error=Exception("Connection refused"),
            target_url="http://example.com/article",
        )

        probe = res.get("network_probe", {})
        assert probe.get("dns_status") == "SUCCESS"
        assert probe.get("tcp_status") == "FAILED"
        assert "Connection refused" in probe.get("tcp_error", "")
        assert "TCP: Failed" in res.get("summary", "")


def test_diagnostics_dns_failure_aborts_tcp():
    """Verify DNS failure aborts immediately with zero TCP connections attempted."""
    with patch("socket.getaddrinfo", side_effect=socket.gaierror(-2, "Name or service not known")), \
         patch("socket.socket") as mock_sock_cls:

        res = collect_failure_diagnostics(
            stage="extraction",
            error=DNSResolutionError("DNS lookup failed"),
            target_url="https://nonexistent.domain.xyz/article",
        )

        probe = res.get("network_probe", {})
        assert probe.get("dns_status") == "FAILED"
        assert probe.get("status") == "DNS_FAILURE"
        assert "DNS: Failed" in res.get("summary", "")
        # TCP socket must NOT be opened or connected
        mock_sock_cls.return_value.connect.assert_not_called()


def test_diagnostics_tcp_success_tls_failure():
    """Verify TCP succeeds but TLS handshake failure is captured."""
    mock_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    with patch("socket.getaddrinfo", return_value=mock_addrinfo), \
         patch("socket.socket") as mock_sock_cls, \
         patch("ssl.create_default_context") as mock_ssl_ctx:
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock

        mock_context = MagicMock()
        mock_context.wrap_socket.side_effect = ssl.SSLCertVerificationError("Certificate verify failed")
        mock_ssl_ctx.return_value = mock_context

        res = collect_failure_diagnostics(
            stage="extraction",
            error=Exception("Certificate verification failed"),
            target_url="https://example.com/article",
        )

        probe = res.get("network_probe", {})
        assert probe.get("dns_status") == "SUCCESS"
        assert probe.get("tcp_status") == "SUCCESS"
        assert probe.get("tls_status") == "FAILED"
        assert "TLS: Failed" in res.get("summary", "")


def test_diagnostics_http_403_blocked():
    """Verify publisher HTTP 403 block is reflected in diagnostic summary."""
    mock_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

    with patch("socket.getaddrinfo", return_value=mock_addrinfo), \
         patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock

        res = collect_failure_diagnostics(
            stage="extraction",
            error=SourceAccessBlockedError("Publisher blocked automated retrieval (HTTP 403): https://example.com"),
            target_url="http://example.com/paywalled",
        )

        assert "HTTP 403" in res.get("summary", "")
        assert res.get("error_category") == "SOURCE_ACCESS_BLOCKED"


def test_diagnostics_ssrf_refusal_zero_connections():
    """Verify SSRF detection on private/loopback/metadata IP aborts with zero TCP connections."""
    prohibited_ips = ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"]

    for ip in prohibited_ips:
        mock_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]
        with patch("socket.getaddrinfo", return_value=mock_addrinfo), \
             patch("socket.socket") as mock_sock_cls:

            res = collect_failure_diagnostics(
                stage="extraction",
                error=SSRFVulnerabilityError(f"Target host resolves to prohibited IP '{ip}'"),
                target_url="http://internal-host.local/admin",
            )

            probe = res.get("network_probe", {})
            assert probe.get("status") == "SSRF_REFUSAL"
            assert probe.get("prohibited_ip") == ip
            assert "SSRF: Blocked" in res.get("summary", "")
            # Critical: verify socket.connect was NEVER called
            mock_sock_cls.return_value.connect.assert_not_called()


def test_diagnostics_ai_model_404_no_network_probe():
    """Verify AI model unavailable 404 captures model diagnostics without URL probing."""
    err = GeminiModelUnavailableError("Gemini model 'gemini-3.6-flash' is not available or not found (404)")

    res = collect_failure_diagnostics(
        stage="ai_script",
        error=err,
    )

    assert "network_probe" not in res
    assert res.get("error_category") == "AI_MODEL_UNAVAILABLE"
    assert "Model Unavailable" in res.get("summary", "")


def test_diagnostics_timeout_bound():
    """Verify diagnostics probes enforce socket timeout bounded by timeout_seconds."""
    mock_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

    with patch("socket.getaddrinfo", return_value=mock_addrinfo), \
         patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock

        collect_failure_diagnostics(
            stage="extraction",
            error=Exception("Timeout"),
            target_url="http://example.com/slow",
            timeout_seconds=1.5,
        )

        assert mock_sock.settimeout.called
        timeout_arg = mock_sock.settimeout.call_args[0][0]
        assert timeout_arg == pytest.approx(1.5, abs=0.05)


def test_diagnostics_multi_attempt_history_preservation(in_memory_db):
    """Verify multiple diagnostic runs append attempt history to job.auto_diagnostics_json."""
    job = PodcastJob(
        id="test-job-multidiag",
        transport="telegram",
        status=JobState.RECEIVED.value,
        source_text="Sample text",
        source_hash="sample_hash_123",
    )
    in_memory_db.add(job)
    in_memory_db.commit()

    # Attempt 1
    collect_failure_diagnostics(
        stage="scripting",
        error=Exception("First temporary failure"),
        job_id=job.id,
        attempt=1,
        db=in_memory_db,
    )

    in_memory_db.refresh(job)
    assert len(job.auto_diagnostics_json) == 1
    assert job.auto_diagnostics_json[0]["attempt"] == 1
    assert "First temporary failure" in job.auto_diagnostics_json[0]["error_message"]

    # Attempt 2
    collect_failure_diagnostics(
        stage="scripting",
        error=Exception("Second failure"),
        job_id=job.id,
        attempt=2,
        db=in_memory_db,
    )

    in_memory_db.refresh(job)
    assert len(job.auto_diagnostics_json) == 2
    assert job.auto_diagnostics_json[0]["attempt"] == 1
    assert job.auto_diagnostics_json[1]["attempt"] == 2
    assert "Second failure" in job.auto_diagnostics_json[1]["error_message"]


def test_diagnostics_redacts_secrets():
    """Verify secrets in error messages and URLs are redacted in diagnostics output."""
    raw_error = Exception("Failed connecting with secret token sk-proj-supersecret1234567890 in URL")

    res = collect_failure_diagnostics(
        stage="scripting",
        error=raw_error,
        target_url="https://api.service.com/query?token=secret123",
    )

    # Error message must be sanitized
    assert "sk-proj-supersecret" not in res["error_message"]
    # Target URL must not contain sensitive token value in plaintext if redacted
    assert "secret123" not in res["target_url"] or "[REDACTED]" in res["target_url"]


class TestFailureDiagnosticsZipExport:
    """Verify failure-diagnostics.json in diagnostics ZIP retains list root and redacts secrets."""

    def test_zip_contains_list_root_failure_diagnostics(self, db_session, tmp_path, monkeypatch):
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

    def test_legacy_diagnostics_zip_repair(self, db_session, tmp_path, monkeypatch):
        """Pre-existing ZIP with legacy failure-diagnostics.json: {} is atomically repaired."""
        import zipfile

        from herald.services.diagnostics_export import (
            ensure_terminal_diagnostics_archive,
            get_terminal_diagnostics_path,
        )

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

        archive_path = get_terminal_diagnostics_path(job.id, job.status)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path, "w") as z:
            z.writestr("failure-diagnostics.json", "{}\n")
            z.writestr("job.json", "{}\n")

        with zipfile.ZipFile(archive_path, "r") as z:
            initial_content = json.loads(z.read("failure-diagnostics.json").decode("utf-8"))
            assert isinstance(initial_content, dict)

        repaired_path = ensure_terminal_diagnostics_archive(job.id, JobState.FAILED_FINAL.value)
        assert repaired_path is not None and Path(repaired_path).exists()

        with zipfile.ZipFile(repaired_path, "r") as z:
            repaired_content = json.loads(z.read("failure-diagnostics.json").decode("utf-8"))
            assert isinstance(repaired_content, list), "Repaired ZIP must have list root"
            assert len(repaired_content) == 1
            assert repaired_content[0]["attempt"] == 1
            assert repaired_content[0]["api_key"] != "sk-secret-do-not-leak"


def test_accurate_ai_model_and_operation_in_failure_diagnostics():
    """
    Verify:
    - Grounded research records provider='gemini', configured_model=settings.GEMINI_RESEARCH_MODEL, operation='grounded_research'.
    - Alternative provider records exact provider name and model.
    - Literal mode records no AI diagnostics.
    """
    res_research = collect_failure_diagnostics(
        stage="research",
        error=Exception("Google Search grounding quota exceeded"),
        provider="gemini",
        model=getattr(settings, "GEMINI_RESEARCH_MODEL", "gemini-3.6-flash"),
        operation="grounded_research",
    )
    ai_diag = res_research.get("ai_diagnostics")
    assert ai_diag is not None
    assert ai_diag["provider"] == "gemini"
    assert ai_diag["configured_model"] == getattr(settings, "GEMINI_RESEARCH_MODEL", "gemini-3.6-flash")
    assert ai_diag["operation"] == "grounded_research"

    res_alt = collect_failure_diagnostics(
        stage="scripting",
        error=Exception("Provider rate limited"),
        provider="openrouter",
        model="anthropic/claude-3.5-sonnet",
        operation="standard_script",
    )
    ai_diag_alt = res_alt.get("ai_diagnostics")
    assert ai_diag_alt is not None
    assert ai_diag_alt["provider"] == "openrouter"
    assert ai_diag_alt["configured_model"] == "anthropic/claude-3.5-sonnet"
    assert ai_diag_alt["operation"] == "standard_script"

    res_literal = collect_failure_diagnostics(
        stage="scripting",
        error=Exception("Text normalization error"),
        operation="literal_script",
    )
    assert res_literal.get("ai_diagnostics") is None


def test_case_d_rerun_approval_failure_card():
    """
    Verify format_generation_failure_card renders:
    - Status: FAILED_FINAL
    - Job ID
    - Concise diagnostic summary
    - Copyable /diagnostics <job-id>
    """
    job = PodcastJob(
        id="job-cased-fail-12345",
        status=JobState.FAILED_FINAL.value,
        error_detail="Script generation LLM call failed",
        auto_diagnostics_json=[{
            "stage": "scripting",
            "error_category": "AI_MODEL_UNAVAILABLE",
            "summary": "Gemini [gemini-3.5-flash]: Model Unavailable (404)",
        }],
    )

    card = format_generation_failure_card(job=job)
    assert "❌ <b>Podcast Generation Failed</b>" in card
    assert "• <b>ID:</b> <code>job-case</code>" in card
    assert f"• <b>Status:</b> <code>{JobState.FAILED_FINAL.value}</code>" in card
    assert "Script generation LLM call failed" in card
    assert "• <b>Diagnostic:</b> Gemini [gemini-3.5-flash]: Model Unavailable (404)" in card
    assert "Use <code>/diagnostics job-case</code> for support details." in card


def test_html_escaping_in_format_concise_failure_summary():
    """
    Verify format_concise_failure_summary HTML-escapes raw summary, stage, and error_category
    to prevent Telegram 400 Bad Request entity parsing errors.
    """
    diag_record = {
        "stage": "ai<script>",
        "error_category": "PARSE_ERROR & ABORT",
        "summary": "Error in <stdin> line 42: <unmatched tag> & invalid <syntax>",
    }

    result = format_concise_failure_summary(diag_record)
    assert "&lt;stdin&gt;" in result
    assert "&lt;unmatched tag&gt;" in result
    assert "&amp;" in result
    assert "<stdin>" not in result
    assert "<unmatched tag>" not in result

    fallback_record = {
        "stage": "ai<script>",
        "error_category": "ERR <TAG> & CRASH",
        "summary": "",
    }
    fb_result = format_concise_failure_summary(fallback_record)
    assert "&lt;SCRIPT&gt;" in fb_result
    assert "ERR &lt;TAG&gt; &amp; CRASH" in fb_result
    assert "<script>" not in fb_result

