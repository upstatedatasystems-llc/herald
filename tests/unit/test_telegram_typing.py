import time
from unittest.mock import MagicMock

from herald.telegram.client import TelegramAPIError
from herald.telegram.typing import TelegramTypingNotifier


def test_immediate_send_on_start():
    mock_client = MagicMock()
    notifier = TelegramTypingNotifier(client=mock_client, chat_id=12345, interval_sec=10.0)

    notifier.start()
    try:
        # Give brief slice for thread start
        time.sleep(0.05)
        mock_client.send_chat_action.assert_called_once_with(chat_id=12345, action="typing")
    finally:
        notifier.stop()


def test_heartbeat_sends_repeatedly():
    mock_client = MagicMock()
    # Fast interval for testing
    notifier = TelegramTypingNotifier(client=mock_client, chat_id=12345, interval_sec=0.1)

    notifier.start()
    try:
        time.sleep(0.35)
        # Should have sent at least 3 times (initial + 2 intervals)
        assert mock_client.send_chat_action.call_count >= 3
    finally:
        notifier.stop()


def test_error_suppression():
    mock_client = MagicMock()
    mock_client.send_chat_action.side_effect = TelegramAPIError("Network timeout")

    notifier = TelegramTypingNotifier(client=mock_client, chat_id=12345, interval_sec=0.1)

    # Should not raise exception
    with notifier:
        time.sleep(0.15)

    assert mock_client.send_chat_action.call_count >= 1


def test_context_manager_lifecycle():
    mock_client = MagicMock()
    notifier = TelegramTypingNotifier(client=mock_client, chat_id=12345, interval_sec=1.0)

    with notifier:
        assert notifier._thread is not None
        assert notifier._thread.is_alive()

    # After exit, thread should be terminated and cleared
    assert notifier._thread is None
