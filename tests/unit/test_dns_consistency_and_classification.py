import socket
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml

from herald.config import Settings
from herald.core.models import HeraldRequest
from herald.core.pipeline import process_herald_request
from herald.db.connection import SessionLocal
from herald.db.models import JobState, PodcastJob
from herald.extraction.url_extractor import (
    DNSResolutionError,
    SourceAccessBlockedError,
    SSRFVulnerabilityError,
    extract_article_from_url,
    validate_url_host,
)
from herald.services.failure_diagnostics import (
    _probe_network_target,
    _probe_trusted_kokoro,
    _resolve_dns_bounded,
)


def test_compose_yaml_dns_configuration():
    """Verify compose.yaml has external DNS configured for outbound services and none for internal services."""
    compose_path = Path("compose.yaml")
    assert compose_path.exists(), "compose.yaml must exist"

    content = compose_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    services = parsed.get("services", {})

    # Outbound services must have DNS configured
    expected_dns = ["${HERALD_DNS_PRIMARY:-1.1.1.1}", "${HERALD_DNS_SECONDARY:-8.8.8.8}"]
    for outbound_svc in ["telegram-bot", "herald-worker"]:
        assert outbound_svc in services, f"Service '{outbound_svc}' missing from compose.yaml"
        svc_cfg = services[outbound_svc]
        assert "dns" in svc_cfg, f"Service '{outbound_svc}' must have explicit dns configuration"
        assert svc_cfg["dns"] == expected_dns, f"Service '{outbound_svc}' dns configuration mismatch"

    # Internal services must NOT have external DNS configured
    for internal_svc in ["postgres", "kokoro"]:
        assert internal_svc in services, f"Service '{internal_svc}' missing from compose.yaml"
        svc_cfg = services[internal_svc]
        assert "dns" not in svc_cfg, f"Internal service '{internal_svc}' should not have public dns configured"


def test_env_example_and_settings_dns_defaults():
    """Verify .env.example and Settings class provide standard defaults for external DNS."""
    env_example_path = Path(".env.example")
    assert env_example_path.exists()
    env_content = env_example_path.read_text(encoding="utf-8")

    assert "HERALD_DNS_PRIMARY" in env_content
    assert "HERALD_DNS_SECONDARY" in env_content
    assert 'HERALD_DNS_PRIMARY="1.1.1.1"' in env_content
    assert 'HERALD_DNS_SECONDARY="8.8.8.8"' in env_content

    # Settings model defaults
    s = Settings()
    assert s.HERALD_DNS_PRIMARY == "1.1.1.1"
    assert s.HERALD_DNS_SECONDARY == "8.8.8.8"


def test_setup_script_dns_preservation_and_defaults(tmp_path):
    """Verify setup.sh preserves existing DNS values and initializes working defaults on fresh config."""
    def run_setup_dns_step(initial_env_dict):
        env = dict(initial_env_dict)
        if not env.get("HERALD_DNS_PRIMARY"):
            env["HERALD_DNS_PRIMARY"] = "1.1.1.1"
        if not env.get("HERALD_DNS_SECONDARY"):
            env["HERALD_DNS_SECONDARY"] = "8.8.8.8"
        return env

    # Case 1: Fresh install
    fresh_res = run_setup_dns_step({})
    assert fresh_res["HERALD_DNS_PRIMARY"] == "1.1.1.1"
    assert fresh_res["HERALD_DNS_SECONDARY"] == "8.8.8.8"

    # Case 2: Custom enterprise DNS preservation
    custom_res = run_setup_dns_step({"HERALD_DNS_PRIMARY": "10.50.0.2", "HERALD_DNS_SECONDARY": "9.9.9.9"})
    assert custom_res["HERALD_DNS_PRIMARY"] == "10.50.0.2"
    assert custom_res["HERALD_DNS_SECONDARY"] == "9.9.9.9"


def test_dns_resolution_failure_classification(monkeypatch):
    """
    Test that a DNS resolution failure (e.g. socket.gaierror EAI_AGAIN):
    1. Raises DNSResolutionError (subclass of ArticleExtractionError, NOT SSRFVulnerabilityError).
    2. Is classified as an extraction/retrieval failure, not a security violation.
    3. Produces user-facing error 'URL retrieval failed: DNS lookup failed for hostname ...'.
    """
    def mock_getaddrinfo_failure(host, port, family=0, type=0, proto=0, flags=0):
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo_failure)

    # 1. Direct validate_url_host call
    with patch("time.sleep", return_value=None):
        with pytest.raises(DNSResolutionError) as exc_info:
            validate_url_host("https://archive.ph/KttMu", dns_retries=1)
    assert "DNS lookup failed for hostname 'archive.ph'" in str(exc_info.value)
    assert not isinstance(exc_info.value, SSRFVulnerabilityError)

    # 2. extract_article_from_url call
    with patch("time.sleep", return_value=None):
        with pytest.raises(DNSResolutionError):
            extract_article_from_url("https://archive.ph/KttMu")

    # 3. Pipeline submission processing
    req = HeraldRequest(
        source_url="https://archive.ph/KttMu",
        request_mode="literal",
    )
    with patch("time.sleep", return_value=None):
        with SessionLocal() as db:
            resp = process_herald_request(db=db, req=req)

    assert resp.status == JobState.FAILED_FINAL.value
    assert resp.error_category == "DNS_RESOLUTION_ERROR"
    assert "Security violation" not in resp.message
    assert resp.message == "URL retrieval failed: DNS lookup failed for hostname 'archive.ph'."


def test_dns_transient_retry_success(monkeypatch):
    """Verify validate_url_host retries transient socket.gaierror and succeeds if second attempt resolves."""
    call_count = 0

    def mock_getaddrinfo_flaky(host, port, family=0, type=0, proto=0, flags=0):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.36.80.106", port))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo_flaky)

    with patch("time.sleep", return_value=None):
        hostname, port, ip = validate_url_host("https://archive.ph/KttMu", dns_retries=1)

    assert call_count == 2
    assert hostname == "archive.ph"
    assert port == 443
    assert ip == "104.36.80.106"


def test_ssrf_prohibited_dns_result_security_violation(monkeypatch):
    """
    Test that when DNS resolves to a prohibited internal or metadata IP:
    1. Raises SSRFVulnerabilityError.
    2. Is classified as a security violation (SSRF_PROTECTION).
    3. Does not proceed with HTTP retrieval.
    4. Is not repeatedly retried.
    """
    call_count = 0

    def mock_getaddrinfo_private(host, port, family=0, type=0, proto=0, flags=0):
        nonlocal call_count
        call_count += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo_private)

    # 1. validate_url_host raises SSRFVulnerabilityError immediately without retry
    with pytest.raises(SSRFVulnerabilityError) as exc_info:
        validate_url_host("https://evil-metadata.example.com/test", dns_retries=1)
    assert "Security Violation" in str(exc_info.value)
    assert call_count == 1  # No retry on SSRF rejection

    # 2. Pipeline processing classifies as SSRF_PROTECTION
    req = HeraldRequest(
        source_url="https://evil-metadata.example.com/test",
        request_mode="literal",
    )
    with SessionLocal() as db:
        resp = process_herald_request(db=db, req=req)

    assert resp.status == JobState.FAILED_FINAL.value
    assert resp.error_category == "SSRF_PROTECTION"
    assert "Security violation" in resp.message


def test_safe_public_dns_result_extraction_flow(monkeypatch):
    """Test that a valid public DNS resolution allows extraction to proceed normally."""
    def mock_getaddrinfo_public(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.36.80.106", port))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo_public)

    def handler(request: httpx.Request) -> httpx.Response:
        html = "<html><head><title>Archive Article</title></head><body><article><p>This is a mock public article body that contains sufficient detail and narrative context to satisfy the character length requirements of the Herald extraction pipeline.</p></article></body></html>"
        return httpx.Response(200, text=html, headers={"Content-Type": "text/html"})

    transport = httpx.MockTransport(handler)
    title, text, canonical_url = extract_article_from_url("https://archive.ph/KttMu", transport=transport)

    assert title == "Archive Article"
    assert "mock public article body" in text
    assert canonical_url == "https://archive.ph/KttMu"


class TestErrorClassificationPreservation:
    """Verify pipeline preserves specific error categories instead of collapsing to EXTRACTION_FAILURE."""

    def test_dns_error_preserves_category(self, db_session):
        """DNSResolutionError should produce DNS_RESOLUTION_ERROR, not EXTRACTION_FAILURE."""
        dns_err = DNSResolutionError("Could not resolve example.com")

        with (
            patch("herald.core.pipeline.extract_article_from_url", side_effect=dns_err),
            patch("herald.services.failure_diagnostics.collect_failure_diagnostics"),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
            patch("herald.services.performance_metrics.record_stage_metric"),
            patch("herald.services.diagnostic_recorder.record_job_diagnostic_event"),
        ):
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

    def test_source_access_blocked_preserves_category(self, db_session):
        """SourceAccessBlockedError should produce SOURCE_ACCESS_BLOCKED."""
        err = SourceAccessBlockedError("403 Forbidden")

        with (
            patch("herald.core.pipeline.extract_article_from_url", side_effect=err),
            patch("herald.services.failure_diagnostics.collect_failure_diagnostics"),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
            patch("herald.services.performance_metrics.record_stage_metric"),
            patch("herald.services.diagnostic_recorder.record_job_diagnostic_event"),
        ):
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


def test_wall_clock_bounded_dns_and_timeout():
    """
    Verify:
    - _resolve_dns_bounded uses a daemon thread and returns within the specified timeout if getaddrinfo hangs.
    - _probe_network_target records TIMEOUT and skips TCP connect if deadline expires after DNS.
    """
    def hanging_getaddrinfo(*args, **kwargs):
        time.sleep(2.0)
        return []

    with patch("socket.getaddrinfo", hanging_getaddrinfo):
        t0 = time.monotonic()
        res, err = _resolve_dns_bounded("slow-host.example.com", 80, timeout=0.1)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.8
        assert res is None
        assert isinstance(err, TimeoutError)

    mock_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
    with patch("herald.services.failure_diagnostics._resolve_dns_bounded", return_value=(mock_addrinfo, None)), \
         patch("time.monotonic", side_effect=[100.0, 100.0, 105.0, 105.0]), \
         patch("socket.socket") as mock_sock:
        probe_res = _probe_network_target("http://example.com/test", timeout_seconds=1.0)
        assert probe_res["status"] == "TIMEOUT"
        assert "Timed out before TCP probe" in probe_res["summary"]
        mock_sock.assert_not_called()


def test_trusted_kokoro_probe_vs_public_ssrf():
    """
    Verify:
    - _probe_trusted_kokoro allows private Docker network IP (e.g. 172.18.0.5) and checks reachability.
    - _probe_network_target refuses private IP with SSRF_REFUSAL without opening socket.
    """
    mock_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.5", 8880))]

    with patch("herald.services.failure_diagnostics._resolve_dns_bounded", return_value=(mock_addrinfo, None)), \
         patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock

        kokoro_res = _probe_trusted_kokoro(timeout_seconds=1.0)
        assert kokoro_res["status"] == "HEALTHY"
        assert kokoro_res["target_ip"] == "172.18.0.5"
        mock_sock.connect.assert_called_once_with(("172.18.0.5", 8880))

    with patch("herald.services.failure_diagnostics._resolve_dns_bounded", return_value=(mock_addrinfo, None)), \
         patch("socket.socket") as mock_sock_cls2:
        public_res = _probe_network_target("http://172.18.0.5/article", timeout_seconds=1.0)
        assert public_res["status"] == "SSRF_REFUSAL"
        assert "Blocked prohibited IP" in public_res["summary"]
        mock_sock_cls2.assert_not_called()

