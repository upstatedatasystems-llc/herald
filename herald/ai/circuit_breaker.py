"""
In-process Provider Availability Circuit Breaker for Herald.

Requirements:
- Trigger ONLY for clearly non-transient provider-account failures (e.g. AIQuotaExhaustedError).
- Do NOT trigger for arbitrary network errors, timeouts, or single malformed responses.
- Short process-local cooldown (default 300s, configurable via AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS).
- Other providers remain usable.
- Provider recovery after cooldown without requiring restart.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

from herald.config import settings

logger = logging.getLogger("herald.ai.circuit_breaker")


@dataclass
class CircuitBreakerEntry:
    provider: str
    tripped_at: float
    cooldown_seconds: float
    reason: str

    @property
    def expires_at(self) -> float:
        return self.tripped_at + self.cooldown_seconds

    @property
    def is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())


_LOCK = Lock()
_TRIPPED_PROVIDERS: dict[str, CircuitBreakerEntry] = {}


def trip_circuit_breaker(
    provider: str,
    reason: str,
    cooldown_seconds: float | None = None,
    job_id: str | None = None,
    db: Any = None,
) -> None:
    """
    Trip the circuit breaker for a provider due to non-transient account/quota exhaustion.
    """
    if not provider:
        return

    p_norm = provider.lower().strip()
    if p_norm in ("literal", "none"):
        return

    cooldown = (
        cooldown_seconds
        if cooldown_seconds is not None
        else float(getattr(settings, "AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 300.0))
    )

    entry = CircuitBreakerEntry(
        provider=p_norm,
        tripped_at=time.monotonic(),
        cooldown_seconds=cooldown,
        reason=reason,
    )

    with _LOCK:
        _TRIPPED_PROVIDERS[p_norm] = entry

    logger.warning(
        f"Circuit breaker TRIPPED for provider '{p_norm}' for {cooldown:.1f}s. Reason: {reason}"
    )

    if job_id and db:
        try:
            from herald.services.diagnostic_recorder import record_job_diagnostic_event

            record_job_diagnostic_event(
                job_id=job_id,
                level="WARNING",
                component="ai_circuit_breaker",
                event_type="CIRCUIT_BREAKER_TRIPPED",
                message=f"Circuit breaker tripped for {p_norm} ({cooldown:.0f}s cooldown): {reason}",
                metadata={
                    "provider": p_norm,
                    "cooldown_seconds": cooldown,
                    "reason": reason,
                },
                db=db,
            )
        except Exception as diag_err:
            logger.debug(f"Failed recording circuit breaker diagnostic event: {diag_err}")


def is_circuit_breaker_active(provider: str) -> tuple[bool, str | None]:
    """
    Check whether the circuit breaker is currently active for the provider.
    Returns (True, reason) if tripped and in cooldown, else (False, None).
    Expired entries are automatically purged.
    """
    if not provider:
        return False, None

    p_norm = provider.lower().strip()

    with _LOCK:
        entry = _TRIPPED_PROVIDERS.get(p_norm)
        if not entry:
            return False, None

        if entry.is_expired:
            del _TRIPPED_PROVIDERS[p_norm]
            logger.info(
                f"Circuit breaker cooldown EXPIRED for provider '{p_norm}'. Provider restored to active rotation."
            )
            return False, None

        return True, f"{entry.reason} (cooldown: {entry.remaining_seconds:.1f}s remaining)"


def reset_circuit_breakers_for_tests() -> None:
    """Clear all circuit breaker states (strictly for unit tests)."""
    with _LOCK:
        _TRIPPED_PROVIDERS.clear()
