
import pytest

from herald.audio.ffmpeg_builder import (
    generate_silence_wav,
    validate_audio_file,
)
from herald.extraction.url_extractor import SSRFVulnerabilityError, is_ip_allowed, validate_url_host


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
