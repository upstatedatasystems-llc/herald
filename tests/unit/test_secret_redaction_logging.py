import io
import logging

from herald.logging import SecretRedactingFormatter, register_secret_for_redaction


def test_secret_redaction_in_emitted_logs():
    """
    Test that secret tokens and API keys are completely redacted from emitted log records.
    """
    secret_bot_token = "7788990011:AAFakeTelegramTokenSecretXYZ"
    secret_gemini_key = "AIzaSyFakeSecretGeminiKey123456789"
    secret_auth_token = "bearer-secret-auth-999"

    register_secret_for_redaction(secret_bot_token, "[REDACTED_BOT_TOKEN]")
    register_secret_for_redaction(secret_gemini_key, "[REDACTED_API_KEY]")
    register_secret_for_redaction(secret_auth_token, "[REDACTED_AUTH]")

    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(SecretRedactingFormatter("%(levelname)s - %(message)s"))

    test_logger = logging.getLogger("test_redaction_logger")
    test_logger.setLevel(logging.DEBUG)
    test_logger.addHandler(handler)

    try:
        # 1. Log containing Telegram URL with token
        test_logger.error(f"Failed to connect to https://api.telegram.org/bot{secret_bot_token}/sendMessage")

        # 2. Log containing Gemini API Key in header
        test_logger.warning(f"Request failed with x-goog-api-key: '{secret_gemini_key}'")

        # 3. Log containing Authorization header
        test_logger.info(f"Auth token used: authorization: '{secret_auth_token}'")

        # 4. Simulated exception
        try:
            raise RuntimeError(f"Connection failed for key {secret_gemini_key} on bot {secret_bot_token}")
        except Exception as e:
            test_logger.exception(e)

        handler.flush()
        captured = log_stream.getvalue()

        # Assert secret tokens and keys DO NOT appear anywhere in the output
        assert secret_bot_token not in captured
        assert secret_gemini_key not in captured
        assert secret_auth_token not in captured

        # Assert redacted placeholders are present
        assert "[REDACTED_BOT_TOKEN]" in captured
        assert "[REDACTED_API_KEY]" in captured
        assert "[REDACTED_AUTH]" in captured
    finally:
        test_logger.removeHandler(handler)


class TestRedactValue:
    """Verify redact_value() handles all JSON-serializable types correctly."""

    def test_list_root_preserved(self):
        """auto_diagnostics_json is a list — redact_value must preserve list structure."""
        from herald.services.redaction import redact_value

        records = [
            {"attempt": 1, "stage": "extraction", "error_message": "blocked"},
            {"attempt": 2, "stage": "extraction", "api_key": "sk-secret123"},
        ]
        result = redact_value(records)
        assert isinstance(result, list), "List root must be preserved"
        assert len(result) == 2
        assert result[0]["attempt"] == 1
        assert result[0]["stage"] == "extraction"

    def test_dict_input_delegates_to_redact_dict(self):
        from herald.services.redaction import redact_dict, redact_value

        d = {"api_key": "secret", "stage": "tts"}
        rv = redact_value(d)
        rd = redact_dict(d)
        assert rv == rd

    def test_string_input_delegates_to_redact_text(self):
        from herald.services.redaction import redact_value

        result = redact_value("my api_key=sk-12345 is here")
        assert isinstance(result, str)

    def test_primitive_passthrough(self):
        from herald.services.redaction import redact_value

        assert redact_value(42) == 42
        assert redact_value(3.14) == 3.14
        assert redact_value(True) is True
        assert redact_value(False) is False
        assert redact_value(None) is None

    def test_nested_list_of_dicts(self):
        from herald.services.redaction import redact_value

        data = [
            {"network_probe": {"dns_ok": True, "tcp_ok": True, "summary": "OK"}},
            {"api_key": "secret-key-value"},
        ]
        result = redact_value(data)
        assert isinstance(result, list)
        assert len(result) == 2
        # network_probe should survive
        assert result[0]["network_probe"]["dns_ok"] is True
        # api_key should be redacted
        assert result[1]["api_key"] != "secret-key-value"

    def test_sensitive_keys_redacted_in_list_items(self):
        """Verify secrets inside list items get redacted."""
        from herald.services.redaction import redact_value

        data = [{"password": "hunter2", "stage": "delivery"}]
        result = redact_value(data)
        assert result[0]["password"] != "hunter2"
        assert result[0]["stage"] == "delivery"

