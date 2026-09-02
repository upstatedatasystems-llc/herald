from herald.config import Settings
from herald.tts.kokoro_client import KokoroClient


def test_kokoro_endpoint_resolution_includes_v1():
    """
    Assert that KokoroClient resolves endpoints with /v1 prefix:
    - http://kokoro:8880/v1/models
    - http://kokoro:8880/v1/audio/speech
    """
    settings = Settings(
        TELEGRAM_BOT_TOKEN="123456:FAKE_TOKEN",
        KOKORO_BASE_URL="http://kokoro:8880/v1",
        AI_PROVIDER="none",
    )
    client = KokoroClient(base_url=settings.KOKORO_BASE_URL)

    assert client.base_url == "http://kokoro:8880/v1"
    models_url = f"{client.base_url}/models"
    speech_url = f"{client.base_url}/audio/speech"

    assert models_url == "http://kokoro:8880/v1/models"
    assert speech_url == "http://kokoro:8880/v1/audio/speech"
