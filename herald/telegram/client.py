import logging
import mimetypes
import re
from pathlib import Path
from typing import Any

import httpx

from herald.config import settings

logger = logging.getLogger("herald.telegram.client")

DEFAULT_DOCUMENT_MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".zip": "application/zip",
    ".md": "text/markdown",
    ".json": "application/json",
    ".txt": "text/plain",
}


class TelegramAPIError(Exception):
    """Exception raised when Telegram Bot API returns an error."""


class TelegramClient:
    """Client for Telegram Bot API using HTTP long polling and direct Bot API methods."""

    def __init__(self, token: str | None = None):
        self._token = (token or settings.TELEGRAM_BOT_TOKEN or "").strip()
        self._base_url = f"https://api.telegram.org/bot{self._token}"

    @property
    def is_configured(self) -> bool:
        return bool(self._token)

    def _sanitize(self, message: str) -> str:
        if self._token and self._token in message:
            return message.replace(self._token, "[REDACTED_BOT_TOKEN]")
        return message

    def _request(self, method: str, endpoint: str, timeout: float = 30.0, **kwargs: Any) -> dict[str, Any]:
        if not self.is_configured:
            raise TelegramAPIError("Telegram bot token is not configured.")

        url = f"{self._base_url}/{endpoint}"
        try:
            with httpx.Client(timeout=timeout) as client:
                if method.upper() == "GET":
                    resp = client.get(url, **kwargs)
                else:
                    resp = client.post(url, **kwargs)

            data = resp.json()
            if not data.get("ok"):
                desc = data.get("description", "Unknown Telegram API error")
                err_code = data.get("error_code", resp.status_code)
                clean_err = self._sanitize(f"Telegram API Error ({err_code}): {desc}")
                raise TelegramAPIError(clean_err)

            return data.get("result", {})
        except TelegramAPIError:
            raise
        except Exception as e:
            clean_err = self._sanitize(f"Telegram HTTP request failed: {e}")
            raise TelegramAPIError(clean_err) from None

    def get_me(self, timeout: float = 10.0) -> dict[str, Any]:
        """Fetch bot info to verify token connectivity."""
        return self._request("GET", "getMe", timeout=timeout)

    def set_my_commands(
        self,
        commands: list[dict[str, str]],
        scope: dict[str, Any] | None = None,
        language_code: str | None = None,
        timeout: float = 15.0,
    ) -> bool:
        """
        Register bot commands via Telegram Bot API setMyCommands.
        Each command dictionary must have 'command' (1-32 chars: a-z, 0-9, _) and 'description' (1-256 chars).
        """
        cmd_pattern = re.compile(r"^[a-z0-9_]{1,32}$")
        validated_cmds = []
        for cmd in commands:
            name = (cmd.get("command") or "").strip().lower()
            desc = (cmd.get("description") or "").strip()
            if not cmd_pattern.match(name):
                raise ValueError(f"Invalid Telegram command name '{name}'. Must match ^[a-z0-9_]{{1,32}}$")
            if not desc or len(desc) > 256:
                raise ValueError(f"Command description for '{name}' must be between 1 and 256 characters.")
            validated_cmds.append({"command": name, "description": desc})

        payload: dict[str, Any] = {"commands": validated_cmds}
        if scope:
            payload["scope"] = scope
        if language_code:
            payload["language_code"] = language_code

        res = self._request("POST", "setMyCommands", timeout=timeout, json=payload)
        return bool(res)

    def get_updates(
        self,
        offset: int | None = None,
        limit: int | None = None,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Long poll for updates from Telegram."""
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        if allowed_updates:
            params["allowed_updates"] = allowed_updates
        else:
            params["allowed_updates"] = ["message", "edited_message"]

        res = self._request("POST", "getUpdates", timeout=float(timeout + 10), json=params)
        return res if isinstance(res, list) else []

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """Send a text message to a Telegram chat."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if parse_mode:
            payload["parse_mode"] = parse_mode

        return self._request("POST", "sendMessage", timeout=15.0, json=payload)

    def send_chat_action(self, chat_id: int | str, action: str = "typing") -> dict[str, Any]:
        """Send chat status action (e.g. typing, upload_document, record_voice)."""
        payload = {"chat_id": chat_id, "action": action}
        return self._request("POST", "sendChatAction", timeout=10.0, json=payload)

    def send_audio(
        self,
        chat_id: int | str,
        audio_path: str | Path,
        caption: str | None = None,
        title: str | None = None,
        performer: str = "Herald",
        duration: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Upload and send an MP3 audio file."""
        p = Path(audio_path)
        if not p.exists():
            raise FileNotFoundError(f"Audio file '{p}' does not exist.")

        data: dict[str, Any] = {
            "chat_id": str(chat_id),
            "performer": performer,
        }
        if caption:
            data["caption"] = caption
        if title:
            data["title"] = title
        if duration is not None:
            data["duration"] = str(int(duration))
        if reply_to_message_id:
            data["reply_to_message_id"] = str(reply_to_message_id)

        url = f"{self._base_url}/sendAudio"
        try:
            with open(p, "rb") as f:
                files = {"audio": (p.name, f, "audio/mpeg")}
                with httpx.Client(timeout=120.0) as client:
                    resp = client.post(url, data=data, files=files)

            res_json = resp.json()
            if not res_json.get("ok"):
                desc = res_json.get("description", "Failed to upload audio")
                raise TelegramAPIError(self._sanitize(f"sendAudio failed ({resp.status_code}): {desc}"))
            return res_json.get("result", {})
        except TelegramAPIError:
            raise
        except Exception as e:
            raise TelegramAPIError(self._sanitize(f"Audio upload exception: {e}")) from None

    def send_document(
        self,
        chat_id: int | str,
        document_path: str | Path,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload and send a document file with dynamic MIME resolution."""
        p = Path(document_path)
        if not p.exists():
            raise FileNotFoundError(f"Document file '{p}' does not exist.")

        if mime_type:
            resolved_mime = mime_type
        else:
            ext = p.suffix.lower()
            resolved_mime = DEFAULT_DOCUMENT_MIME_TYPES.get(ext) or mimetypes.guess_type(str(p))[0] or "application/octet-stream"

        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        if reply_to_message_id:
            data["reply_to_message_id"] = str(reply_to_message_id)

        url = f"{self._base_url}/sendDocument"
        try:
            with open(p, "rb") as f:
                files = {"document": (p.name, f, resolved_mime)}
                with httpx.Client(timeout=60.0) as client:
                    resp = client.post(url, data=data, files=files)

            res_json = resp.json()
            if not res_json.get("ok"):
                desc = res_json.get("description", "Failed to upload document")
                raise TelegramAPIError(self._sanitize(f"sendDocument failed ({resp.status_code}): {desc}"))
            return res_json.get("result", {})
        except TelegramAPIError:
            raise
        except Exception as e:
            raise TelegramAPIError(self._sanitize(f"Document upload exception: {e}")) from None
