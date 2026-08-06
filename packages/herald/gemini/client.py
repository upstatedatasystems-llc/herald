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
    return "Transform the provided source content into a podcast script JSON matching Appendix C schema."


def generate_podcast_script(
    source_text: str,
    request_mode: str = "standard",
    source_title: str | None = None,
    source_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
) -> PodcastScriptResponse:
    """
    Call Gemini API to generate a structured podcast script matching Appendix C schema.
    Enforces retry backoff, schema repair prompt on attempt 2, and prompt injection bounds.
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

Generate the podcast script JSON response matching Appendix C schema now.
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

    # Appendix C JSON Schema Definition
    schema_dict = {
        "type": "OBJECT",
        "properties": {
            "episode_title": {"type": "STRING"},
            "episode_description": {"type": "STRING"},
            "estimated_minutes": {"type": "INTEGER"},
            "source_title": {"type": "STRING"},
            "source_url": {"type": "STRING"},
            "segments": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "order": {"type": "INTEGER"},
                        "heading": {"type": "STRING"},
                        "narration": {"type": "STRING"},
                    },
                    "required": ["order", "heading", "narration"],
                },
            },
            "warnings": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
        },
        "required": [
            "episode_title",
            "episode_description",
            "estimated_minutes",
            "segments",
            "warnings",
        ],
    }

    max_attempts = settings.GEMINI_RETRY_COUNT
    backoff = 2.0
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"Sending script request to Gemini API ({model}), attempt {attempt}/{max_attempts}")
            
            prompt_content = f"{system_prompt}\n\n{user_prompt}"
            if attempt == 2 and last_error:
                # Add schema repair instruction on attempt 2 retry
                prompt_content += f"\n\nNOTE: The previous attempt failed validation with error: '{last_error}'. Please ensure all required fields (episode_title, episode_description, estimated_minutes, segments, warnings) and segment required fields (order, heading, narration) are strictly present."

            payload = {
                "contents": [
                    {"role": "user", "parts": [{"text": prompt_content}]}
                ],
                "generationConfig": {
                    "temperature": settings.GEMINI_TEMPERATURE,
                    "maxOutputTokens": settings.GEMINI_MAX_OUTPUT_TOKENS,
                    "responseMimeType": "application/json",
                    "responseSchema": schema_dict,
                },
            }

            with httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS) as client:
                resp = client.post(url, json=payload)

            if resp.status_code in (401, 403):
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

            # Validate against Appendix C Pydantic schema
            validated_script = PodcastScriptResponse(**script_data)
            return validated_script

        except (json.JSONDecodeError, GeminiValidationError) as e:
            last_error = str(e)
            logger.error(f"Failed to parse or validate Gemini JSON output on attempt {attempt}: {e}")
            if attempt == max_attempts:
                raise GeminiValidationError(f"Invalid JSON/schema returned by Gemini: {e}")
        except Exception as e:
            if isinstance(e, (GeminiAuthError, GeminiQuotaError, GeminiValidationError)):
                raise
            last_error = str(e)
            logger.error(f"Gemini client error on attempt {attempt}: {e}")
            if attempt == max_attempts:
                raise GeminiError(f"Gemini script generation failed: {e}")

        time.sleep(backoff)
        backoff *= 2.0

    raise GeminiError("Failed to generate podcast script from Gemini API after retries.")
