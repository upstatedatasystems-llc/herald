"""
Groq Cloud AI Provider Implementation for Herald.
Reuses OpenAI-compatible schema with Groq API endpoint and specialized
Groq rate limit, context limit, and authentication error mapping.
"""

import httpx

from herald.ai.errors import (
    AIAuthFailedError,
    AIContextExceededError,
    AIPermissionDeniedError,
    AIProviderError,
    AIProviderUnavailableError,
    AIRateLimitedError,
    AIRequestTooLargeError,
)
from herald.ai.openai_provider import OpenAIProvider
from herald.config import settings


class GroqProvider(OpenAIProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        super().__init__(
            api_key=api_key or settings.GROQ_API_KEY,
            model=model or settings.GROQ_MODEL or "llama-3.3-70b-versatile",
            api_base="https://api.groq.com/openai/v1",
            provider_name="Groq",
        )

    def _classify_http_error(
        self,
        resp: httpx.Response,
        attempt: int = 1,
        max_attempts: int = 1,
        operation: str = "script_generation",
    ) -> float | None:
        """
        Classify Groq HTTP error responses into standard AIProviderError taxonomy.
        Specifically maps:
        - HTTP 413 -> AIRequestTooLargeError (not context exceeded)
        - HTTP 400/422 context length exceeded -> AIContextExceededError
        - HTTP 429 or rate limit exceeded -> AIRateLimitedError with Retry-After header
        - HTTP 401 / 403 -> AIAuthFailedError / AIPermissionDeniedError
        - HTTP 5xx -> AIProviderUnavailableError
        """
        status = resp.status_code
        text_preview = resp.text[:300] if resp.text else ""
        text_lower = text_preview.lower()

        # 401 / 403 Authentication failures
        if status == 401:
            raise AIAuthFailedError(
                f"Groq API authentication failed: HTTP 401 ({text_preview})",
                provider="groq",
                model=self._model,
                http_status=401,
                operation=operation,
            )
        if status == 403:
            raise AIPermissionDeniedError(
                f"Groq API permission denied: HTTP 403 ({text_preview})",
                provider="groq",
                model=self._model,
                http_status=403,
                operation=operation,
            )

        # 413 Payload Too Large (Item 8: HTTP 413 -> AIRequestTooLargeError, NOT context exceeded)
        if status == 413:
            raise AIRequestTooLargeError(
                f"Groq request payload too large: HTTP 413 ({text_preview})",
                provider="groq",
                model=self._model,
                http_status=413,
                operation=operation,
            )

        # Context length exceeded (400/422 with context limit signatures)
        if status in (400, 422) and any(
            phrase in text_lower
            for phrase in (
                "context_length_exceeded",
                "maximum context length",
                "too many tokens",
                "context window",
                "tokens exceed context",
            )
        ):
            raise AIContextExceededError(
                f"Groq context limit exceeded: HTTP {status} ({text_preview})",
                provider="groq",
                model=self._model,
                http_status=status,
                operation=operation,
            )

        # 429 Rate Limit
        is_rate_limit = (status == 429) or ("rate_limit_exceeded" in text_lower or "rate limit" in text_lower)
        if is_rate_limit:
            retry_delay = None
            retry_header = resp.headers.get("retry-after")
            if retry_header:
                try:
                    retry_delay = float(retry_header)
                except ValueError:
                    pass
            if retry_delay is None:
                reset_tokens = resp.headers.get("x-ratelimit-reset-tokens")
                if reset_tokens:
                    try:
                        if reset_tokens.endswith("ms"):
                            retry_delay = float(reset_tokens[:-2]) / 1000.0
                        elif reset_tokens.endswith("s"):
                            retry_delay = float(reset_tokens[:-1])
                        else:
                            retry_delay = float(reset_tokens)
                    except ValueError:
                        pass
            raise AIRateLimitedError(
                f"Groq API rate limit exceeded: HTTP {status} ({text_preview})",
                provider="groq",
                model=self._model,
                http_status=status,
                retry_after_seconds=retry_delay,
                operation=operation,
            )

        if status >= 500:
            raise AIProviderUnavailableError(
                f"Groq API returned HTTP {status}",
                provider="groq",
                model=self._model,
                http_status=status,
                operation=operation,
            )

        raise AIProviderError(
            f"Groq API returned HTTP {status}",
            provider="groq",
            model=self._model,
            http_status=status,
            operation=operation,
        )

