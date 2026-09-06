import pytest

from herald.extraction.url_extractor import (
    SSRFVulnerabilityError,
    is_ip_allowed,
    validate_url_host,
)


def test_is_ip_allowed():
    # Public IPs should pass
    assert is_ip_allowed("8.8.8.8") is True
    assert is_ip_allowed("1.1.1.1") is True
    assert is_ip_allowed("93.184.216.34") is True

    # Loopback IPs should fail
    assert is_ip_allowed("127.0.0.1") is False
    assert is_ip_allowed("127.0.0.2") is False

    # Private RFC 1918 IPs should fail
    assert is_ip_allowed("10.0.0.1") is False
    assert is_ip_allowed("172.16.0.1") is False
    assert is_ip_allowed("192.168.1.1") is False

    # Link-local & Metadata IPs should fail
    assert is_ip_allowed("169.254.169.254") is False
    assert is_ip_allowed("169.254.1.1") is False


def test_validate_url_host_blocks_invalid_schemes():
    with pytest.raises(SSRFVulnerabilityError, match="Unsupported scheme"):
        validate_url_host("file:///etc/passwd")

    with pytest.raises(SSRFVulnerabilityError, match="Unsupported scheme"):
        validate_url_host("gopher://127.0.0.1")


def test_validate_url_host_blocks_localhost():
    with pytest.raises(SSRFVulnerabilityError, match="strictly prohibited"):
        validate_url_host("http://localhost:8000")

    with pytest.raises(SSRFVulnerabilityError, match="strictly prohibited"):
        validate_url_host("http://localhost.localdomain/api")


def test_validate_url_host_blocks_credentials_and_ports():
    with pytest.raises(SSRFVulnerabilityError, match="embedded user credentials"):
        validate_url_host("https://user:pass@example.com/article")

    with pytest.raises(SSRFVulnerabilityError, match="Invalid URL port number"):
        validate_url_host("https://example.com:99999/article")


def test_validate_url_host_dns_resolution_error_classification(monkeypatch):
    import socket
    from unittest.mock import patch
    from herald.extraction.url_extractor import DNSResolutionError

    def mock_gai_fail(host, port, family=0, type=0, proto=0, flags=0):
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", mock_gai_fail)

    with patch("time.sleep", return_value=None):
        with pytest.raises(DNSResolutionError, match="DNS lookup failed for hostname 'archive.ph'"):
            validate_url_host("https://archive.ph/test", dns_retries=1)


def test_validate_url_host_ssrf_prohibited_ip(monkeypatch):
    import socket

    def mock_metadata_ip(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_metadata_ip)

    with pytest.raises(SSRFVulnerabilityError, match="Security Violation"):
        validate_url_host("https://cloud-metadata.internal/test", dns_retries=1)
