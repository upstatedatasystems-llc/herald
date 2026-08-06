import pytest

from packages.herald.extraction.url_extractor import (
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
