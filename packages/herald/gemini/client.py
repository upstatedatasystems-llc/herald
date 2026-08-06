import json
import logging
import time
from pathlib import Path

import httpx

from packages.herald.config import settings
from packages.herald.gemini.schema import PodcastScriptResponse

logger = logging.getLogger("herald.gemini")


class GeminiError(Exception):
    """Base exception for Gemini API integration failures."""


class GeminiAuthError(GeminiError):
    """API key or authentication failure."""


class GeminiQuotaError(GeminiError):
    """Rate limit or quota exceeded failure."""


class GeminiValidationError(GeminiError):
    """Returned response failed schema validation."""


def load_system_prompt() -> str:
    prompt_file = Path(__file__).parent.parent.parent.parent / "prompts" / "podcast_script" / "prompt.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    # Fallback prompt if file is missing
    return "Transform the provided source content into a podcast script JSON."


def generate_podcast_script(
    source_text: str,
    request_mode: str = "standard",
    source_title: str | None = None,
    source_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
) -> PodcastScriptResponse:
    """
    Call Gemini API to generate a structured podcast script from source text.
    Enforces retry backoff, JSON schema validation, and prompt injection bounds.
    """
    key = api_key or settings.GEMINI_API_KEY
    model = model_name or settings.GEMINI_MODEL

    if not key:
        raise GeminiAuthError("Gemini API key is not configured.")

    system_prompt = load_system_prompt()

    user_prompt = f"""
REQUESTED MODE: {request_mode.upper()}
SOURCE TITLE: {source_title or 'N/A'}
SOURCE URL: {source_url or 'N/A'}

<SOURCE_DATA>
{source_text}
</SOURCE_DATA>

Generate the podcast script JSON response matching the required schema now.
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

    # Define schema for structured output request
    schema_dict = {
        "type": "OBJECT",
        "properties": {
            "episode_title": {"type": "STRING"},
            "episode_description": {"type": "STRING"},
            "source_title": {"type": "STRING"},
            "source_url": {"type": "STRING"},
            "requested_mode": {"type": "STRING"},
            "segments": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "sequence": {"type": "INTEGER"},
                        "speaker": {"type": "STRING"},
                        "text": {"type": "STRING"},
                    },
                    "required": ["sequence", "speaker", "text"],
                },
            },
        },
        "required": [
            "episode_title",
            "episode_description",
            "requested_mode",
            "segments",
        ],
    }

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": f"{system_prompt}\n\n{user_prompt}"}]}
        ],
        "generationConfig": {
            "temperature": settings.GEMINI_TEMPERATURE,
            "maxOutputTokens": settings.GEMINI_MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
            "responseSchema": schema_dict,
        },
    }

    max_attempts = settings.GEMINI_RETRY_COUNT
    backoff = 2.0

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"Sending script request to Gemini API ({model}), attempt {attempt}/{max_attempts}")
            with httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                resp = client.post(url, json=payload)

            if resp.status_code == 401 or resp.status_code == 403:
                raise GeminiAuthError(f"Gemini API authentication failed ({resp.status_code}): {resp.text}")
            elif resp.status_code == 429:
                if attempt < max_attempts:
                    logger.warning(f"Gemini rate limited (429). Retrying in {backoff} seconds...")
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiQuotaError(f"Gemini API rate limit exceeded: {resp.text}")
            elif resp.status_code != 200:
                if attempt < max_attempts:
                    logger.warning(f"Gemini API error ({resp.status_code}). Retrying in {backoff} seconds...")
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                raise GeminiError(f"Gemini API error ({resp.status_code}): {resp.text}")

            result_json = resp.json()
            candidates = result_json.get("candidates", [])
            if not candidates:
                raise GeminiValidationError("Gemini API returned no response candidates.")

            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts or "text" not in parts[0]:
                raise GeminiValidationError("Gemini candidate content missing text part.")

            raw_text = parts[0]["text"]
            script_data = json.loads(raw_text)

            # Validate against Pydantic schema
            validated_script = PodcastScriptResponse(**script_data)
            return validated_script

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Gemini JSON output on attempt {attempt}: {e}")
            if attempt == max_attempts:
                raise GeminiValidationError(f"Invalid JSON returned by Gemini: {e}")
        except Exception as e:
            if isinstance(e, (GeminiAuthError, GeminiQuotaError, GeminiValidationError)):
                raise
            logger.error(f"Gemini client error on attempt {attempt}: {e}")
            if attempt == max_attempts:
                raise GeminiError(f"Gemini script generation failed: {e}")

        time.sleep(backoff)
        backoff *= 2.0

    raise GeminiError("Failed to generate podcast script from Gemini API after retries.")
