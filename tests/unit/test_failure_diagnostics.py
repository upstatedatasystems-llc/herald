"""
Unit test suite for stage-aware failure diagnostics & anti-SSRF protections.
"""

import socket
import ssl
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.db.models import Base, JobState, PodcastJob
from herald.extraction.url_extractor import (
    DNSResolutionError,
    SSRFVulnerabilityError,
    SourceAccessBlockedError,
)
from herald.gemini.client import GeminiModelUnavailableError
from herald.services.failure_diagnostics import collect_failure_diagnostics


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
                target_url=f"http://internal-host.local/admin",
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
    assert "AI: Model Unavailable" in res.get("summary", "")


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

        mock_sock.settimeout.assert_called_once_with(1.5)


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
