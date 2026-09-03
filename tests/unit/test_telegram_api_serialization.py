from apps.api.main import JobStatusResponse


def test_job_status_response_serialization_with_telegram_fields():
    """
    Test that JobStatusResponse correctly serializes Telegram jobs with null Gmail fields
    and native integer Telegram IDs.
    """
    resp = JobStatusResponse(
        id="job-tg-12345",
        transport="telegram",
        telegram_chat_id=123456789,
        telegram_message_id=98765,
        telegram_user_id=123456789,
        request_mode="literal",
        source_type="text",
        source_url=None,
        status="complete",
        attempt_count=1,
        synthesis_attempt_count=1,
        delivery_attempt_count=1,
        completed_chunk_index=3,
        local_audio_path="/data/herald/audio/job-tg-12345.mp3",
        audio_bytes=1048576,
        audio_sha256="abcdef123456",
        audio_duration_seconds=120,
        drive_file_id=None,
        drive_web_link=None,
        drive_job_key=None,
        gmail_result_message_id=None,
        kokoro_voice="af_heart",
        kokoro_speed=1.0,
        gemini_model=None,
        error_code=None,
        error_detail=None,
        created_at="2026-09-02T18:00:00Z",
        updated_at="2026-09-02T18:02:00Z",
    )

    data = resp.model_dump()
    assert data["transport"] == "telegram"
    assert data["telegram_chat_id"] == 123456789
    assert data["telegram_message_id"] == 98765
    assert data["gmail_message_id"] is None
    assert data["sender_email"] is None


def test_telegram_client_get_updates_limit_contract(monkeypatch):
    """
    Test that TelegramClient.get_updates accepts limit argument and sends it in payload.
    """
    import httpx

    from herald.telegram.client import TelegramClient

    sent_params = {}

    def mock_post(self, url, **kwargs):
        nonlocal sent_params
        sent_params = kwargs.get("json", {})
        return httpx.Response(200, json={"ok": True, "result": []})

    monkeypatch.setattr(httpx.Client, "post", mock_post)

    client = TelegramClient(token="123456:TEST_TOKEN")
    updates = client.get_updates(offset=100, limit=50, timeout=10)

    assert updates == []
    assert sent_params.get("offset") == 100
    assert sent_params.get("limit") == 50
    assert sent_params.get("timeout") == 10
