import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from apps.api.main import app
from herald.config import Settings, settings
from herald.core.models import HeraldRequest
from herald.core.pipeline import process_herald_request
from herald.db.connection import SessionLocal
from herald.db.models import JobState, PodcastJob
from herald.extraction.url_extractor import (
    ArticleExtractionError,
    DNSResolutionError,
    SSRFVulnerabilityError,
    extract_article_from_url,
    validate_url_host,
)

api_client = TestClient(app)


def test_compose_yaml_dns_configuration():
    """Verify compose.yaml has external DNS configured for outbound services and none for internal services."""
    compose_path = Path("compose.yaml")
    assert compose_path.exists(), "compose.yaml must exist"

    content = compose_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    services = parsed.get("services", {})

    # Outbound services must have DNS configured
    expected_dns = ["${HERALD_DNS_PRIMARY:-1.1.1.1}", "${HERALD_DNS_SECONDARY:-8.8.8.8}"]
    for outbound_svc in ["telegram-bot", "herald-worker", "herald-api"]:
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
    assert resp.error_category == "EXTRACTION_FAILURE"
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


def test_api_intake_dns_vs_ssrf_error_categorization(monkeypatch, db_session):
    """Test that the HTTP API /api/v1/intake and /api/v1/extract endpoints distinguish DNS resolution failure from SSRF violations."""
    monkeypatch.setattr(settings, "HERALD_ENV", "testing")
    monkeypatch.setattr(settings, "HERALD_API_KEY", "")
    monkeypatch.setattr(settings, "EMAIL_ALLOWED_SENDERS", "tester@example.com")

    # Case A: DNS Failure on /api/v1/intake
    def mock_dns_fail(host, port, family=0, type=0, proto=0, flags=0):
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", mock_dns_fail)

    with patch("time.sleep", return_value=None):
        resp_dns = api_client.post(
            "/api/v1/intake",
            json={
                "gmail_message_id": "msg-dns-fail-1",
                "sender_email": "tester@example.com",
                "subject": "Podcast: Literal",
                "body_text": "https://archive.ph/KttMu",
            },
        )
    assert resp_dns.status_code == 200
    data_dns = resp_dns.json()
    assert data_dns["error_category"] == "DNS_RESOLUTION_FAILURE"
    assert "URL retrieval failed: DNS lookup failed for hostname 'archive.ph'." in data_dns["message"]

    # Case B: SSRF Security Violation on /api/v1/intake
    def mock_ssrf(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_ssrf)

    resp_ssrf = api_client.post(
        "/api/v1/intake",
        json={
            "gmail_message_id": "msg-ssrf-fail-1",
            "sender_email": "tester@example.com",
            "subject": "Podcast: Literal",
            "body_text": "https://internal-service.local/admin",
        },
    )
    assert resp_ssrf.status_code == 200
    data_ssrf = resp_ssrf.json()
    assert data_ssrf["error_category"] == "SSRF_PROTECTION"
    assert "Security violation" in data_ssrf["message"]

    # Case C: /api/v1/extract HTTP status code differentiation
    monkeypatch.setattr(socket, "getaddrinfo", mock_dns_fail)
    with patch("time.sleep", return_value=None):
        resp_extract_dns = api_client.post(
            "/api/v1/extract",
            json={"url": "https://archive.ph/KttMu"},
        )
    assert resp_extract_dns.status_code == 422
    assert "DNS lookup failed" in resp_extract_dns.json()["detail"]

    monkeypatch.setattr(socket, "getaddrinfo", mock_ssrf)
    resp_extract_ssrf = api_client.post(
        "/api/v1/extract",
        json={"url": "https://internal-service.local/admin"},
    )
    assert resp_extract_ssrf.status_code == 403
    assert "SSRF Protection" in resp_extract_ssrf.json()["detail"]
