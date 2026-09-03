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
