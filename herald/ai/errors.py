"""
Normalized AI error taxonomy for Herald AI providers.
Ensures provider-neutral error classification, retry decisions, and safe diagnostics.
"""

from typing import Any


class AIProviderError(RuntimeError):
    """Base exception for all Herald external AI provider failures."""

    def __init__(
        self,
        message: str,
        category: str = "AI_PROVIDER_ERROR",
        provider: str | None = None,
        model: str | None = None,
        http_status: int | None = None,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
        operation: str | None = None,
        safe_detail: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.category = category
        self.provider = provider
        self.model = model
        self.http_status = http_status
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.operation = operation
        self.safe_detail = safe_detail or message

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "provider": self.provider,
            "model": self.model,
            "http_status": self.http_status,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "operation": self.operation,
            "safe_detail": self.safe_detail,
        }


class AIRateLimitedError(AIProviderError):
    """Provider rate limit or quota exceeded (HTTP 429)."""

    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("category", "AI_RATE_LIMITED")
        kwargs.setdefault("http_status", 429)
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class AIProviderTimeoutError(AIProviderError):
    """Server-side or upstream gateway timeout (HTTP 408 / 504)."""

    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("category", "AI_PROVIDER_TIMEOUT")
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class AIClientTimeoutError(AIProviderError):
    """Client-side request timeout while waiting for provider response."""

    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("category", "AI_CLIENT_TIMEOUT")
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class AIAuthFailedError(AIProviderError):
    """Authentication or credential failure (HTTP 401). Never retried on same provider."""

    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("category", "AI_AUTH_FAILED")
        kwargs.setdefault("http_status", 401)
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


class AIPermissionDeniedError(AIProviderError):
    """Permission denied or unauthorized model access (HTTP 403). Never retried on same provider."""

    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("category", "AI_PERMISSION_DENIED")
        kwargs.setdefault("http_status", 403)
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


class AIModelUnavailableError(AIProviderError):
    """Requested model not available, retired, or invalid."""

    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("category", "AI_MODEL_UNAVAILABLE")
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


class AIProviderUnavailableError(AIProviderError):
    """Provider transient outage or 5xx server error."""

    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("category", "AI_PROVIDER_UNAVAILABLE")
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class AIRequestTooLargeError(AIProviderError):
    """Payload or prompt exceeds provider request body limit (HTTP 413)."""

    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("category", "AI_REQUEST_TOO_LARGE")
        kwargs.setdefault("http_status", 413)
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


class AIContextExceededError(AIProviderError):
    """Input tokens exceed model context window limit."""

    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("category", "AI_CONTEXT_EXCEEDED")
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


class AISchemaInvalidError(AIProviderError):
    """Provider returned malformed JSON or response violating schema."""

    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("category", "AI_SCHEMA_INVALID")
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class AIUnsupportedCapabilityError(AIProviderError):
    """Candidate does not support requested capability (e.g. research_grounding)."""

    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("category", "UNSUPPORTED_CAPABILITY")
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


class AIOutputTruncatedError(AIProviderError):
    """Provider response was cut off before completion due to max token limits."""

    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("category", "AI_OUTPUT_TRUNCATED")
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class AIChainExhaustedError(AIProviderError):
    """All snapshotted provider candidates in the chain failed or were skipped."""

    def __init__(self, message: str, failures: list[dict[str, Any]] | None = None, **kwargs):
        kwargs.setdefault("category", "AI_CHAIN_EXHAUSTED")
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)
        self.failures = failures or []


# Backward compatibility and ergonomic taxonomy aliases
AIAuthenticationError = AIAuthFailedError
AIContextLimitExceededError = AIContextExceededError
AIRateLimitError = AIRateLimitedError

