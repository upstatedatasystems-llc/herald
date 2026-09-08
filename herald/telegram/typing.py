"""
Telegram typing chat action heartbeat service.
Maintains continuous 'typing...' feedback in Telegram during URL extraction
and AI script generation. Strictly excluded from TTS synthesis stages.
"""

import logging
import threading
from typing import Any

logger = logging.getLogger("herald.telegram.typing")


class TelegramTypingNotifier:
    """
    Background heartbeat manager that sends Telegram sendChatAction('typing')
    every interval_sec (default: 4.0s).

    Safe to use as a context manager:
        with TelegramTypingNotifier(client, chat_id):
            extract_url()
            generate_script()

    Guarantees:
    1. Initial typing action sent immediately upon start.
    2. Background thread daemonized and terminated cleanly within timeout.
    3. Network/API errors during chat action are completely non-fatal.
    """

    def __init__(
        self,
        client: Any,
        chat_id: int | str,
        interval_sec: float = 4.0,
    ) -> None:
        self.client = client
        self.chat_id = chat_id
        self.interval_sec = max(0.05, float(interval_sec))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _send_action(self) -> None:
        try:
            if self.client and hasattr(self.client, "send_chat_action"):
                self.client.send_chat_action(chat_id=self.chat_id, action="typing")
        except Exception as e:
            logger.debug(f"Non-fatal Telegram typing action heartbeat failed: {e}")

    def _run(self) -> None:
        # Immediate initial send
        self._send_action()
        while not self._stop_event.wait(timeout=self.interval_sec):
            self._send_action()

    def start(self) -> "TelegramTypingNotifier":
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"TelegramTypingHeartbeat-{self.chat_id}",
                daemon=True,
            )
            self._thread.start()
            return self

    def stop(self, timeout: float = 1.0) -> None:
        with self._lock:
            if self._thread is None:
                return
            self._stop_event.set()
            thread = self._thread
            self._thread = None

        if thread.is_alive():
            thread.join(timeout=timeout)

    def __enter__(self) -> "TelegramTypingNotifier":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()
