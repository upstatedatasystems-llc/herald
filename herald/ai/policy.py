"""
Central retry, backoff, failover, and adaptation budget policies for Herald AI providers.
Ensures identical classification-driven rules across all providers without duplication.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from herald.ai.errors import (
    AIAuthFailedError,
    AIChainExhaustedError,
    AIClientTimeoutError,
    AIContextExceededError,
    AIModelUnavailableError,
    AIOutputTruncatedError,
    AIPermissionDeniedError,
    AIProviderError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
    AIRateLimitedError,
    AIRequestTooLargeError,
    AISchemaInvalidError,
    AIUnsupportedCapabilityError,
)


@dataclass
class AdaptationBudget:
    """Explicit, typed bounds for multi-stage large source adaptation."""

    max_chunks: int = 12
    max_reduction_depth: int = 2
    max_ai_calls: int = 15
    max_retry_calls: int = 3
    max_estimated_work: int = 500_000
    max_elapsed_seconds: float = 600.0

    @classmethod
    def from_settings(cls, cfg: Any = None) -> "AdaptationBudget":
        if cfg is None:
            from herald.config import settings as cfg
        return cls(
            max_chunks=getattr(cfg, "ADAPTATION_MAX_CHUNKS", 12),
            max_reduction_depth=getattr(cfg, "ADAPTATION_MAX_DEPTH", 2),
            max_ai_calls=getattr(cfg, "ADAPTATION_MAX_AI_CALLS", 15),
            max_retry_calls=getattr(cfg, "ADAPTATION_MAX_RETRY_CALLS", 3),
            max_estimated_work=getattr(cfg, "ADAPTATION_MAX_ESTIMATED_WORK", 500_000),
            max_elapsed_seconds=float(getattr(cfg, "ADAPTATION_MAX_ELAPSED_SECONDS", 600.0)),
        )


@dataclass
class AdaptationUsage:
    """Tracks work consumed across adaptation stages and persists across provider failover."""

    chunks_processed: int = 0
    reduction_depth: int = 0
    ai_calls: int = 0
    retry_calls: int = 0
    estimated_work: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def elapsed_seconds(self) -> float:
        return (datetime.now(UTC) - self.started_at).total_seconds()

    def check_budget(self, budget: AdaptationBudget) -> None:
        if self.chunks_processed > budget.max_chunks:
            raise AIRequestTooLargeError(
                f"Source exceeds maximum adaptation chunk budget ({self.chunks_processed} > {budget.max_chunks})"
            )
        if self.reduction_depth > budget.max_reduction_depth:
            raise AIRequestTooLargeError(
                f"Adaptation exceeds maximum reduction depth ({self.reduction_depth} > {budget.max_reduction_depth})"
            )
        if self.ai_calls >= budget.max_ai_calls:
            raise AIProviderUnavailableError(
                f"Adaptation budget exhausted ({self.ai_calls} calls >= limit {budget.max_ai_calls})"
            )
        if self.elapsed_seconds > budget.max_elapsed_seconds:
            raise AIProviderTimeoutError(
                f"Adaptation time budget exceeded ({self.elapsed_seconds:.1f}s > {budget.max_elapsed_seconds}s)"
            )


class ActionType:
    RETRY_SAME_PROVIDER = "RETRY_SAME_PROVIDER"
    FAILOVER_NEXT_PROVIDER = "FAILOVER_NEXT_PROVIDER"
    ADAPT_LARGE_SOURCE = "ADAPT_LARGE_SOURCE"
    FAIL_FINAL = "FAIL_FINAL"


@dataclass
class RetryDecision:
    action: str
    backoff_seconds: float = 0.0
    reason: str = ""
    error: AIProviderError | None = None


def extract_retry_after(headers: Any) -> float | None:
    """Safely extract and parse Retry-After header in seconds."""
    if not headers:
        return None
    val = headers.get("retry-after") or headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except (ValueError, TypeError):
        return None


def classify_error(
    err: Exception,
    provider: str,
    model: str | None = None,
    operation: str | None = None,
) -> AIProviderError:
    """
    Classify any raw exception or HTTP error into a normalized AIProviderError.
    Preserves provider identity and sanitized details.
    """
    if isinstance(err, AIProviderError):
        if not err.provider:
            err.provider = provider
        if not err.model:
            err.model = model
        if not err.operation:
            err.operation = operation
        return err

    msg = str(err)
    low_msg = msg.lower()

    # Network / Timeout errors
    if isinstance(err, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return AIClientTimeoutError(
            f"Client timeout connecting to {provider}: {msg}",
            provider=provider,
            model=model,
            operation=operation,
            safe_detail="AI client timeout",
        )
    if isinstance(err, httpx.NetworkError):
        return AIProviderUnavailableError(
            f"Network connection failed to {provider}: {msg}",
            provider=provider,
            model=model,
            operation=operation,
            safe_detail="AI network connection failed",
        )

    # HTTP status code inspection
    status_code: int | None = None
    headers: Any = None
    if isinstance(err, httpx.HTTPStatusError) and err.response is not None:
        status_code = err.response.status_code
        headers = err.response.headers

    # HTTP 429 Rate Limiting
    if status_code == 429 or "rate limit" in low_msg or "too many requests" in low_msg or "quota" in low_msg:
        ra = extract_retry_after(headers)
        return AIRateLimitedError(
            f"{provider} rate limit exceeded: {msg}",
            provider=provider,
            model=model,
            operation=operation,
            retry_after_seconds=ra,
            safe_detail="AI provider rate limit reached",
        )

    # HTTP 408 / 504 Timeout
    if status_code in (408, 504) or "gateway timeout" in low_msg:
        return AIProviderTimeoutError(
            f"{provider} request timed out (HTTP {status_code}): {msg}",
            provider=provider,
            model=model,
            operation=operation,
            http_status=status_code,
            safe_detail=f"AI provider timeout (HTTP {status_code})",
        )

    # HTTP 401 Auth Failed
    if status_code == 401 or "unauthorized" in low_msg or "invalid api key" in low_msg or "authentication" in low_msg:
        return AIAuthFailedError(
            f"{provider} authentication failed: {msg}",
            provider=provider,
            model=model,
            operation=operation,
            safe_detail=f"{provider} credentials invalid or missing",
        )

    # HTTP 403 Permission Denied
    if status_code == 403 or "forbidden" in low_msg or "permission denied" in low_msg or "permissions_error" in low_msg:
        return AIPermissionDeniedError(
            f"{provider} permission denied: {msg}",
            provider=provider,
            model=model,
            operation=operation,
            safe_detail=f"{provider} access to model forbidden",
        )

    # HTTP 413 Payload Too Large
    if status_code == 413 or "payload too large" in low_msg or "request entity too large" in low_msg:
        return AIRequestTooLargeError(
            f"{provider} request too large (HTTP 413): {msg}",
            provider=provider,
            model=model,
            operation=operation,
            safe_detail="AI request payload exceeded provider limit",
        )

    # Context window / token overflow
    if "context length" in low_msg or "maximum context" in low_msg or "context_window_exceeded" in low_msg or "token limit" in low_msg:
        return AIContextExceededError(
            f"{provider} context window exceeded: {msg}",
            provider=provider,
            model=model,
            operation=operation,
            safe_detail="Source text exceeded AI model context window",
        )

    # Model not found / unavailable
    if status_code == 404 or "model not found" in low_msg or "does not exist" in low_msg or "model_not_found" in low_msg or "decommissioned" in low_msg:
        return AIModelUnavailableError(
            f"{provider} model '{model}' unavailable: {msg}",
            provider=provider,
            model=model,
            operation=operation,
            http_status=status_code,
            safe_detail=f"Model '{model}' is unavailable on {provider}",
        )

    # 5xx Server Errors
    if status_code is not None and 500 <= status_code < 600:
        return AIProviderUnavailableError(
            f"{provider} server error (HTTP {status_code}): {msg}",
            provider=provider,
            model=model,
            operation=operation,
            http_status=status_code,
            safe_detail=f"AI provider server error (HTTP {status_code})",
        )

    # Truncation
    if "finish_reason: length" in low_msg or "max_tokens reached" in low_msg or "output truncated" in low_msg:
        return AIOutputTruncatedError(
            f"{provider} output truncated: {msg}",
            provider=provider,
            model=model,
            operation=operation,
            safe_detail="AI output reached max tokens before completion",
        )

    # Schema / Parsing
    if "json" in low_msg and ("decode" in low_msg or "schema" in low_msg or "validation" in low_msg or "expecting" in low_msg):
        return AISchemaInvalidError(
            f"{provider} response schema invalid: {msg}",
            provider=provider,
            model=model,
            operation=operation,
            safe_detail="AI response violated expected schema",
        )

    # Fallback generic provider error
    return AIProviderError(
        f"{provider} error: {msg}",
        provider=provider,
        model=model,
        operation=operation,
        http_status=status_code,
        safe_detail=f"AI provider error: {msg[:100]}",
    )


def decide_policy(
    error: AIProviderError,
    attempt: int,
    max_attempts: int,
    has_next_candidate: bool,
    adaptation_available: bool = True,
) -> RetryDecision:
    """
    Central decision engine determining whether to:
    1. Adapt large source (for 413 / context exceeded on same provider)
    2. Retry on same provider (for transient 429, timeout, 5xx, schema repair)
    3. Failover to next snapshotted candidate (when same provider retry exhausted or deterministic auth/permission)
    4. Fail final (when candidates exhausted or non-retryable fatal error)
    """
    # 1. Non-failover errors: internal programmer bugs / malformed application state
    # (These will not be AIProviderError or will have UNSUPPORTED_CAPABILITY handled elsewhere)

    # 2. Large source errors: 413 Request Too Large or Context Exceeded
    if isinstance(error, (AIRequestTooLargeError, AIContextExceededError)):
        if adaptation_available:
            return RetryDecision(
                action=ActionType.ADAPT_LARGE_SOURCE,
                reason="Large source detected; attempting same-provider adaptation first",
                error=error,
            )
        if has_next_candidate:
            return RetryDecision(
                action=ActionType.FAILOVER_NEXT_PROVIDER,
                reason="Adaptation cannot resolve size limits on current provider; advancing candidate",
                error=error,
            )
        return RetryDecision(action=ActionType.FAIL_FINAL, reason="Source exceeds provider limits", error=error)

    # 3. Deterministic Non-Retryable Errors on Current Provider: 401 Auth, 403 Permission, Model Unavailable
    if isinstance(error, (AIAuthFailedError, AIPermissionDeniedError, AIModelUnavailableError)):
        if has_next_candidate:
            return RetryDecision(
                action=ActionType.FAILOVER_NEXT_PROVIDER,
                reason=f"Deterministic failure on current provider ({error.category}); failing over immediately",
                error=error,
            )
        return RetryDecision(
            action=ActionType.FAIL_FINAL,
            reason=f"Deterministic failure ({error.category}) and no fallback candidates",
            error=error,
        )

    # 4. Same-provider bounded retry: 429 Rate Limited
    if isinstance(error, AIRateLimitedError):
        if attempt < max_attempts:
            backoff = error.retry_after_seconds if error.retry_after_seconds is not None else (2.0 ** attempt)
            # Cap backoff to 30.0s to avoid exceeding request deadline
            backoff = min(30.0, max(1.0, backoff))
            return RetryDecision(
                action=ActionType.RETRY_SAME_PROVIDER,
                backoff_seconds=backoff,
                reason=f"Rate limited (attempt {attempt}/{max_attempts}); retrying after {backoff:.1f}s",
                error=error,
            )
        # Same-provider retry budget exhausted
        if has_next_candidate:
            return RetryDecision(
                action=ActionType.FAILOVER_NEXT_PROVIDER,
                reason=f"Rate limit exhausted on current provider after {attempt} attempts; failing over",
                error=error,
            )
        return RetryDecision(action=ActionType.FAIL_FINAL, reason="Rate limit exhausted", error=error)

    # 5. Same-provider bounded retry: Timeouts & 5xx Provider Unavailable
    if isinstance(error, (AIProviderTimeoutError, AIClientTimeoutError, AIProviderUnavailableError)):
        if attempt < max_attempts:
            backoff = 2.0 ** attempt
            return RetryDecision(
                action=ActionType.RETRY_SAME_PROVIDER,
                backoff_seconds=backoff,
                reason=f"Transient failure {error.category} (attempt {attempt}/{max_attempts}); retrying",
                error=error,
            )
        if has_next_candidate:
            return RetryDecision(
                action=ActionType.FAILOVER_NEXT_PROVIDER,
                reason=f"Transient failure exhausted on current provider after {attempt} attempts; failing over",
                error=error,
            )
        return RetryDecision(action=ActionType.FAIL_FINAL, reason="Attempts exhausted", error=error)

    # 6. Schema Invalid: 1 bounded repair
    if isinstance(error, AISchemaInvalidError):
        if attempt < max_attempts:
            return RetryDecision(
                action=ActionType.RETRY_SAME_PROVIDER,
                backoff_seconds=1.0,
                reason="Schema invalid; attempting bounded repair",
                error=error,
            )
        if has_next_candidate:
            return RetryDecision(
                action=ActionType.FAILOVER_NEXT_PROVIDER,
                reason="Schema repair exhausted on current provider; failing over",
                error=error,
            )
        return RetryDecision(action=ActionType.FAIL_FINAL, reason="Schema invalid", error=error)

    # 7. Output Truncated
    if isinstance(error, AIOutputTruncatedError):
        if attempt < max_attempts:
            return RetryDecision(
                action=ActionType.RETRY_SAME_PROVIDER,
                backoff_seconds=1.0,
                reason="Output truncated; attempting recovery",
                error=error,
            )
        if has_next_candidate:
            return RetryDecision(
                action=ActionType.FAILOVER_NEXT_PROVIDER,
                reason="Output truncation unrecoverable on current provider; failing over",
                error=error,
            )
        return RetryDecision(action=ActionType.FAIL_FINAL, reason="Output truncated", error=error)

    # Default fallback
    if has_next_candidate:
        return RetryDecision(
            action=ActionType.FAILOVER_NEXT_PROVIDER,
            reason=f"Error {error.category}; failing over to next candidate",
            error=error,
        )
    return RetryDecision(action=ActionType.FAIL_FINAL, reason=error.message, error=error)
