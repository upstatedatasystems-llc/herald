import socket

import httpx
import pytest

from herald.audio.ffmpeg_builder import (
    FFmpegExecutionError,
    generate_silence_wav,
    validate_audio_file,
)
from herald.extraction.url_extractor import (
    SSRFVulnerabilityError,
    extract_article_from_url,
    is_ip_allowed,
    validate_url_host,
)


def test_ssrf_ip_blocking():
    # IPv4 Private & Loopback
    assert is_ip_allowed("127.0.0.1") is False
    assert is_ip_allowed("10.0.0.1") is False
    assert is_ip_allowed("192.168.1.1") is False
    assert is_ip_allowed("169.254.169.254") is False

    # IPv4-mapped IPv6 Loopback / Metadata
    assert is_ip_allowed("::ffff:127.0.0.1") is False
    assert is_ip_allowed("::ffff:169.254.169.254") is False

    # Safe Public IPs
    assert is_ip_allowed("8.8.8.8") is True
    assert is_ip_allowed("1.1.1.1") is True


def test_validate_url_host_blocks_localhost():
    with pytest.raises(SSRFVulnerabilityError):
        validate_url_host("http://localhost:8000/secret")


def test_audio_validation_and_silence(tmp_path):
    silence_file = tmp_path / "silence.wav"
    generate_silence_wav(silence_file, duration_seconds=0.5)

    assert silence_file.exists()
    assert silence_file.stat().st_size > 0

    val = validate_audio_file(silence_file)
    assert val["valid"] is True
    assert val["size_bytes"] > 0


def test_invalid_audio_rejection(tmp_path):
    # 1. 0-byte file
    empty_file = tmp_path / "empty.wav"
    empty_file.touch()
    with pytest.raises(FFmpegExecutionError):
        validate_audio_file(empty_file)

    # 2. Non-audio text file
    junk_file = tmp_path / "junk.wav"
    junk_file.write_bytes(b"This is not audio content")
    with pytest.raises(FFmpegExecutionError):
        validate_audio_file(junk_file)


def test_mocked_extract_article_ssrf(monkeypatch):
    """Verify extract_article_from_url uses mocked DNS and MockTransport with no live network calls."""
    def mock_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        html = "<html><head><title>Test Article</title></head><body><article><p>This is a sufficiently long mock article text for testing extraction without network calls. It contains detailed narrative background context and statistics to meet the minimum character threshold of 100 characters.</p></article></body></html>"
        return httpx.Response(200, text=html, headers={"Content-Type": "text/html"})

    transport = httpx.MockTransport(handler)
    title, text, canonical_url = extract_article_from_url("https://public-test.example.com/news", transport=transport)

    assert title == "Test Article"
    assert "mock article text" in text
    assert canonical_url == "https://public-test.example.com/news"
