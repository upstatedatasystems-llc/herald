from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
import httpx
import pytest

from herald.config import settings
from herald.tts.kokoro_client import KokoroClient


def test_kokoro_health_successful_probe():
    client = KokoroClient()
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), patch("httpx.Client.get", return_value=mock_resp):
        res = client.health_check()
        assert res["healthy"] is True
        assert res["kokoro_api"] is True
        assert res["degraded"] is False
        assert KokoroClient._last_successful_probe_at is not None


def test_kokoro_health_transient_inference_timeout():
    client = KokoroClient()
    now = datetime.now(UTC)
    KokoroClient._last_successful_probe_at = now - timedelta(seconds=30)  # 30s ago (< 120s grace)

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), patch("httpx.Client.get", side_effect=httpx.TimeoutException("Read timeout")):
        res = client.health_check()
        assert res["healthy"] is True
        assert res["kokoro_api"] is True
        assert res["degraded"] is True


def test_kokoro_health_expired_grace():
    client = KokoroClient()
    now = datetime.now(UTC)
    KokoroClient._last_successful_probe_at = now - timedelta(seconds=150)  # 150s ago (> 120s grace)

    with patch("httpx.Client.get", side_effect=httpx.TimeoutException("Read timeout")):
        res = client.health_check()
        assert res["healthy"] is False
        assert res["kokoro_api"] is False


def test_kokoro_health_genuine_unavailable():
    client = KokoroClient()
    KokoroClient._last_successful_probe_at = datetime.now(UTC)
    mock_resp = MagicMock()
    mock_resp.status_code = 500

    with patch("httpx.Client.get", return_value=mock_resp):
        res = client.health_check()
        assert res["healthy"] is False
        assert res["kokoro_api"] is False
