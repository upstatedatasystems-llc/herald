"""
Deterministic Multi-Provider Failover Executor for Herald.
Enforces:
1. Strict candidate chain execution derived from the job's immutable snapshot.
2. Same-provider remediation first.
3. Sticky failover within the job (never cycling backwards).
4. Durable cursor persistence (job.ai_failover_index) for restart recovery.
5. Capability-aware skipping without downgrading requested mode.
6. Safe preflight logging and failover telemetry with zero secret/source leakage.
"""

import json
import logging
import time
from typing import Any, Callable

from herald.ai.adaptation import adapt_source_text
from herald.ai.errors import (
    AIChainExhaustedError,
    AIContextExceededError,
    AIRequestTooLargeError,
)
from herald.ai.policy import (
    ActionType,
    AdaptationBudget,
    AdaptationUsage,
    classify_error,
    decide_policy,
)
from herald.ai.registry import create_provider, get_descriptor, is_provider_configured
from herald.config import settings
from herald.db.models import AIInteraction, PodcastJob
from herald.services.diagnostic_recorder import record_job_diagnostic_event

logger = logging.getLogger("herald.ai.failover")


def record_ai_preflight(
    job_id: str | None,
    provider: str,
    model: str,
    operation: str,
    source_text: str | None,
    attempt: int,
    failover_index: int,
    adaptation_mode: str = "DIRECT",
    db: Any = None,
) -> dict[str, Any]:
    """
    Calculate and record safe preflight telemetry before an external AI request.
    Never logs source bodies, source prompts, credentials, or secrets.
    """
    text = source_text or ""
    source_chars = len(text)
    source_bytes = len(text.encode("utf-8"))
    # Heuristic token estimate ~ 4 chars per token for English
    estimated_tokens = max(1, source_chars // 4)

    desc = get_descriptor(provider)
    known_context: int | None = None
    known_max_output: int | None = None
    known_body_limit: int | None = None
    reasoning_effort: str | None = None

    if desc:
        for m in desc.catalog_models:
            if m.model_id == model:
                known_context = m.context_window
                known_max_output = m.max_output
                known_body_limit = m.known_request_body_limit
                reasoning_effort = m.model_specific_defaults.get("reasoning_effort")
                break

    op_instructions = {
        "script_generation": 2200,
        "grounded_research": 1800,
        "research_normalization": 2500,
        "research_script": 2800,
        "research_audit": 1900,
        "research_repair": 2100,
        "verification": 1600,
        "verification_repair": 1800,
        "adaptation": 800,
    }
    instruction_len = op_instructions.get(operation, 1500)

    op_max_output = {
        "grounded_research": 8192,
        "research_normalization": getattr(settings, "GEMINI_RESEARCH_MAX_OUTPUT_TOKENS", 16384),
        "research_script": getattr(settings, "GEMINI_MAX_OUTPUT_TOKENS", 8192),
        "script_generation": getattr(settings, "GEMINI_MAX_OUTPUT_TOKENS", 8192),
        "verification": 4096,
        "verification_repair": 8192,
        "adaptation": 4096,
    }
    req_output = op_max_output.get(operation, 8192)
    if known_max_output:
        req_output = min(req_output, known_max_output)

    estimated_envelope = {
        "model": model,
        "messages": [
            {"role": "system", "content": "X" * instruction_len},
            {"role": "user", "content": text},
        ],
        "max_tokens": req_output,
    }
    serialized_req_bytes = len(json.dumps(estimated_envelope).encode("utf-8"))

    preflight_meta = {
        "provider": provider,
        "model": model,
        "operation": operation,
        "source_characters": source_chars,
        "source_utf8_bytes": source_bytes,
        "serialized_request_bytes": serialized_req_bytes,
        "estimated_input_tokens": estimated_tokens,
        "known_context_limit": known_context,
        "known_output_limit": known_max_output,
        "requested_max_output": req_output,
        "known_request_body_limit": known_body_limit,
        "attempt": attempt,
        "configured_timeout_seconds": settings.effective_ai_timeout_seconds,
        "reasoning_effort": reasoning_effort,
        "adaptation_mode": adaptation_mode,
        "failover_chain_index": failover_index,
    }

    if job_id and db:
        record_job_diagnostic_event(
            job_id=job_id,
            level="DEBUG",
            component="ai_preflight",
            event_type="AI_REQUEST_PREFLIGHT",
            message=f"Preflight check for {provider}/{model} ({operation}, attempt {attempt})",
            metadata=preflight_meta,
            db=db,
        )

    # Detect known context overflow before invocation
    if known_context and estimated_tokens > known_context:
        raise AIContextExceededError(
            f"Estimated input tokens ({estimated_tokens}) exceed {provider}/{model} context limit ({known_context})",
            provider=provider,
            model=model,
            operation=operation,
            safe_detail="Preflight detected context limit exceeded",
        )

    # Detect known body limit overflow before invocation
    if known_body_limit and source_bytes > known_body_limit:
        raise AIRequestTooLargeError(
            f"Source bytes ({source_bytes}) exceed {provider} request body limit ({known_body_limit})",
            provider=provider,
            model=model,
            operation=operation,
            safe_detail="Preflight detected request body limit exceeded",
        )

    return preflight_meta


def get_job_provider_chain(job: PodcastJob) -> list[dict[str, str]]:
    """
    Extract immutable provider candidate chain from job snapshot.
    Falls back cleanly to primary provider/model columns or isolated legacy recovery.
    Never returns ambiguous provider in executable chain.
    """
    raw_chain = getattr(job, "ai_provider_chain_json", None)
    if raw_chain and isinstance(raw_chain, list) and len(raw_chain) > 0:
        candidates = [
            {"provider": str(c.get("provider", "")).lower().strip(), "model": str(c.get("model", "")).strip()}
            for c in raw_chain
            if isinstance(c, dict) and c.get("provider") and str(c.get("provider", "")).lower().strip() != "ambiguous"
        ]
        if candidates:
            return candidates

    # Fallback from primary columns for legacy jobs
    prov = getattr(job, "ai_provider", None)
    if prov and str(prov).lower().strip() != "ambiguous":
        p_clean = str(prov).lower().strip()
        mod = getattr(job, "ai_model", None) or ""
        return [{"provider": p_clean, "model": mod}]

    # Isolated legacy fallback only (historical jobs with gemini_model)
    from herald.ai.legacy_compat import resolve_legacy_job_identity

    leg_prov, leg_model = resolve_legacy_job_identity(job)
    if leg_prov and leg_model:
        return [{"provider": leg_prov, "model": leg_model}]

    # Server default chain recovery for ambiguous / missing chains
    from herald.ai.resolution import get_server_default_chain
    try:
        def_chain = get_server_default_chain()
        return [{"provider": c.provider_id, "model": c.model_id} for c in def_chain]
    except Exception:
        return [{"provider": "literal", "model": "none"}]


def _call_execute_fn(fn: Callable[..., Any], provider: Any, attempt: int, source_text: str | None) -> Any:
    """Invoke execute_fn, passing source_text if supported, or falling back to (provider, attempt)."""
    import inspect
    take_three = None
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        take_three = has_varargs or len(params) >= 3
    except (ValueError, TypeError):
        take_three = None

    if take_three is True:
        return fn(provider, attempt, source_text)
    if take_three is False:
        return fn(provider, attempt)

    try:
        return fn(provider, attempt, source_text)
    except TypeError as e:
        if "positional argument" in str(e):
            return fn(provider, attempt)
        raise


def execute_with_failover(
    job: PodcastJob,
    operation: str,
    execute_fn: Callable[..., Any],
    source_text: str | None = None,
    required_capability: str | None = None,
    max_same_provider_attempts: int | None = None,
    db: Any = None,
) -> Any:
    """
    Execute an AI logical operation across the job's snapshotted provider chain.
    Parameters:
        job: The PodcastJob containing snapshotted provider chain and durable cursor.
        operation: Logical operation name (e.g. 'script_generation', 'research_grounding', 'url_context_extraction', 'verification').
        execute_fn: Callable accepting (provider_instance, attempt_number, source_text) returning result.
                    The source_text argument carries the current (possibly adapted) source so that
                    adapted text is always forwarded to the provider — never read from job.source_text.
        source_text: Optional source text for safe preflight calculations.
        required_capability: Optional ProviderCapabilities field name required for operation.
        max_same_provider_attempts: Max attempts on same provider before failover.
        db: Database session for persisting failover transitions and cursor.
    """
    chain = get_job_provider_chain(job)
    if not chain:
        raise AIChainExhaustedError("Job has no configured AI provider candidates in chain", failures=[])

    if source_text is None:
        source_text = getattr(job, "source_text", None)

    # Literal mode short-circuit
    if chain[0]["provider"] == "literal" or (getattr(job, "request_mode", "") == "literal"):
        prov = create_provider("literal")
        return _call_execute_fn(execute_fn, prov, 1, source_text)

    max_attempts = max_same_provider_attempts or getattr(settings, "AI_REQUEST_MAX_ATTEMPTS", 3)
    curr_index = max(0, int(getattr(job, "ai_failover_index", 0) or 0))
    failures_log: list[dict[str, Any]] = []

    # Bounded adaptation budget persists across failover without resetting
    adaptation_budget = AdaptationBudget.from_settings()
    adaptation_usage = AdaptationUsage()

    while curr_index < len(chain):
        prev_index = curr_index
        cand = chain[curr_index]
        p_id = cand["provider"]
        m_id = cand["model"]
        desc = get_descriptor(p_id)

        # 1. Capability-aware validation: does candidate support required capability?
        if required_capability and desc:
            cap_val = getattr(desc.capabilities, required_capability, False)
            if not cap_val:
                logger.info(
                    f"Candidate {p_id} lacks required capability '{required_capability}'; skipping candidate."
                )
                skip_info = {
                    "provider": p_id,
                    "model": m_id,
                    "reason": "UNSUPPORTED_CAPABILITY",
                    "detail": f"Provider lacks required capability '{required_capability}'",
                }
                failures_log.append(skip_info)
                if db:
                    record_job_diagnostic_event(
                        job_id=job.id,
                        level="WARNING",
                        component="ai_failover",
                        event_type="UNSUPPORTED_CAPABILITY",
                        message=f"Candidate {p_id} lacks capability '{required_capability}', advancing chain",
                        metadata=skip_info,
                        db=db,
                    )
                curr_index += 1
                job.ai_failover_index = curr_index
                if db:
                    db.commit()
                continue

        # 2. Check credentials configuration
        if not is_provider_configured(p_id):
            logger.warning(f"Candidate {p_id} credentials are not configured or missing; skipping candidate.")
            cfg_fail = {
                "provider": p_id,
                "model": m_id,
                "reason": "AI_AUTH_FAILED",
                "detail": f"Provider {p_id} credentials are not configured",
            }
            failures_log.append(cfg_fail)
            if db:
                record_job_diagnostic_event(
                    job_id=job.id,
                    level="WARNING",
                    component="ai_failover",
                    event_type="AI_AUTH_FAILED",
                    message=f"Candidate {p_id} not configured, advancing chain",
                    metadata=cfg_fail,
                    db=db,
                )
            curr_index += 1
            job.ai_failover_index = curr_index
            if db:
                db.commit()
            continue

        # 3. Instantiate provider for execution with immutable research model snapshot
        res_model = getattr(job, "research_model", None)
        prov_instance = create_provider(p_id, model_id=m_id, research_model=res_model)

        # 4. Same-provider execution & bounded retry loop
        attempt = 1

        while attempt <= max_attempts:
            # Preflight checks
            try:
                record_ai_preflight(
                    job_id=job.id,
                    provider=p_id,
                    model=m_id,
                    operation=operation,
                    source_text=source_text,
                    attempt=attempt,
                    failover_index=curr_index,
                    db=db,
                )
            except (AIContextExceededError, AIRequestTooLargeError) as size_err:
                logger.warning(f"Preflight size limit exceeded on {p_id}/{m_id}: {size_err}")
                try:
                    logger.info(f"Attempting preflight large-source adaptation for job {job.id} on {p_id}/{m_id}")
                    adapted_text = adapt_source_text(
                        source_text=source_text or getattr(job, "source_text", "") or "",
                        provider=prov_instance,
                        budget=adaptation_budget,
                        usage=adaptation_usage,
                        job_id=job.id,
                        source_title=getattr(job, "custom_title", None),
                        db=db,
                    )
                    # Canonical job.source_text is NEVER overwritten by adapted content
                    source_text = adapted_text
                    continue
                except (TypeError, ValueError, AttributeError, NameError, KeyError, IndexError, SyntaxError, AssertionError):
                    # Programmer errors and local validation bugs must never trigger failover
                    raise
                except Exception as adapt_err:
                    logger.warning(f"Preflight adaptation failed on {p_id}/{m_id}: {adapt_err}")
                    classified_adapt = classify_error(
                        adapt_err, provider=p_id, model=m_id, operation=f"{operation}_adaptation"
                    )
                    has_next = (curr_index + 1) < len(chain)
                    adapt_decision = decide_policy(
                        error=classified_adapt,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        has_next_candidate=has_next,
                    )
                    failures_log.append({
                        "provider": p_id,
                        "model": m_id,
                        "reason": classified_adapt.category,
                        "detail": f"{classified_adapt.safe_detail} (adaptation failed: {adapt_err})",
                    })
                    if adapt_decision.action == ActionType.FAIL_FINAL or not has_next:
                        raise classified_adapt
                    break  # Break inner loop to advance to next candidate

            try:
                t0 = time.monotonic()
                result = _call_execute_fn(execute_fn, prov_instance, attempt, source_text)
                elapsed_ms = int((time.monotonic() - t0) * 1000)

                # Sticky Failover: current candidate becomes active provider for job
                job.ai_effective_provider = p_id
                job.ai_effective_model = m_id
                job.ai_failover_index = curr_index
                if db:
                    db.commit()
                    if getattr(job, "id", None):
                        actual_in = getattr(result, "prompt_tokens", None)
                        actual_out = getattr(result, "completion_tokens", None)
                        if actual_in is None and getattr(prov_instance, "last_usage", None):
                            actual_in = prov_instance.last_usage.get("prompt_tokens")
                            actual_out = prov_instance.last_usage.get("completion_tokens")
                        if actual_in is None and getattr(job, "id", None):
                            latest_inter = (
                                db.query(AIInteraction)
                                .filter(AIInteraction.job_id == job.id)
                                .order_by(AIInteraction.started_at.desc())
                                .first()
                            )
                            if latest_inter:
                                actual_in = latest_inter.prompt_tokens
                                actual_out = latest_inter.completion_tokens

                        record_job_diagnostic_event(
                            job_id=job.id,
                            level="INFO",
                            component="ai_failover",
                            event_type="AI_OPERATION_SUCCESS",
                            message=f"AI operation '{operation}' succeeded on {p_id} ({m_id})",
                            metadata={
                                "provider": p_id,
                                "model": m_id,
                                "operation": operation,
                                "attempt": attempt,
                                "elapsed_ms": elapsed_ms,
                                "failover_chain_index": curr_index,
                                "actual_input_tokens": actual_in,
                                "actual_output_tokens": actual_out,
                            },
                            db=db,
                        )

                logger.info(
                    f"AI operation '{operation}' succeeded on {p_id} ({m_id}) in {elapsed_ms}ms (slot {curr_index})"
                )
                return result

            except (TypeError, AttributeError, NameError, KeyError, IndexError, SyntaxError, AssertionError, ValueError):
                # Programmer errors and internal application bugs must never trigger failover
                raise
            except Exception as e:
                classified = classify_error(e, provider=p_id, model=m_id, operation=operation)
                has_next = (curr_index + 1) < len(chain)
                decision = decide_policy(
                    error=classified,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    has_next_candidate=has_next,
                )

                logger.warning(
                    f"AI execution error on {p_id}/{m_id} (attempt {attempt}/{max_attempts}): "
                    f"category={classified.category}, decision={decision.action} ({decision.reason})"
                )

                if decision.action == ActionType.ADAPT_LARGE_SOURCE:
                    try:
                        logger.info(f"Attempting large-source adaptation after error on {p_id}/{m_id} for job {job.id}")
                        adapted_text = adapt_source_text(
                            source_text=source_text or getattr(job, "source_text", "") or "",
                            provider=prov_instance,
                            budget=adaptation_budget,
                            usage=adaptation_usage,
                            job_id=job.id,
                            source_title=getattr(job, "custom_title", None),
                            db=db,
                        )
                        # Canonical job.source_text is NEVER overwritten by adapted content
                        source_text = adapted_text
                        attempt += 1
                        continue
                    except (TypeError, ValueError, AttributeError, NameError, KeyError, IndexError, SyntaxError, AssertionError):
                        # Programmer errors and local validation bugs must never trigger failover
                        raise
                    except Exception as adapt_err:
                        logger.warning(f"Large-source adaptation failed on {p_id}/{m_id}: {adapt_err}")
                        classified_adapt = classify_error(
                            adapt_err, provider=p_id, model=m_id, operation=f"{operation}_adaptation"
                        )
                        adapt_decision = decide_policy(
                            error=classified_adapt,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            has_next_candidate=has_next,
                        )
                        if adapt_decision.action == ActionType.FAIL_FINAL or not has_next:
                            raise classified_adapt
                        decision = adapt_decision

                if decision.action == ActionType.RETRY_SAME_PROVIDER:
                    if decision.backoff_seconds > 0:
                        time.sleep(decision.backoff_seconds)
                    attempt += 1
                    continue

                elif decision.action == ActionType.FAILOVER_NEXT_PROVIDER:
                    next_cand = chain[curr_index + 1]
                    transition_meta = {
                        "from_provider": p_id,
                        "from_model": m_id,
                        "to_provider": next_cand["provider"],
                        "to_model": next_cand["model"],
                        "reason": classified.category,
                        "attempt": attempt,
                        "operation": operation,
                        "safe_detail": classified.safe_detail,
                    }
                    failures_log.append({
                        "provider": p_id,
                        "model": m_id,
                        "reason": classified.category,
                        "detail": classified.safe_detail,
                    })
                    if db:
                        record_job_diagnostic_event(
                            job_id=job.id,
                            level="WARNING",
                            component="ai_failover",
                            event_type="AI_FAILOVER_TRANSITION",
                            message=f"Failover from {p_id} to {next_cand['provider']}: {classified.category}",
                            metadata=transition_meta,
                            db=db,
                        )
                    # Advance failover cursor
                    curr_index += 1
                    job.ai_failover_index = curr_index
                    if db:
                        db.commit()
                    break  # Break inner loop to start with next candidate

                else:
                    # FAIL_FINAL: record failure
                    failures_log.append({
                        "provider": p_id,
                        "model": m_id,
                        "reason": classified.category,
                        "detail": classified.safe_detail,
                    })
                    if db:
                        record_job_diagnostic_event(
                            job_id=job.id,
                            level="ERROR",
                            component="ai_failover",
                            event_type="AI_FAIL_FINAL",
                            message=f"Terminal failure on {p_id}: {decision.reason}",
                            metadata={
                                "provider": p_id,
                                "model": m_id,
                                "category": classified.category,
                                "safe_detail": classified.safe_detail,
                                "attempt": attempt,
                            },
                            db=db,
                        )
                    # If this is not the last candidate in the chain, FAIL_FINAL must terminate immediately
                    # without advancing to Secondary/Tertiary — cursor stays at current index.
                    if has_next:
                        raise classified
                    # If there are no next candidates and we have exhausted the chain:
                    if len(failures_log) > 1:
                        break  # Breaks inner loop to outer loop termination -> AIChainExhaustedError
                    raise classified

        # If inner loop exhausted same-provider attempts without success (RETRY_SAME_PROVIDER exhausted
        # or ADAPT failed and didn't raise) — advance cursor for FAILOVER path only
        if curr_index == prev_index:
            curr_index += 1
            job.ai_failover_index = curr_index
            if db:
                db.commit()

    # All candidates in snapshotted chain exhausted
    exhausted_summary = "\n".join(
        f"{i+1}. {f['provider']} — {f['reason']} ({f['detail']})"
        for i, f in enumerate(failures_log)
    )
    final_msg = f"AI providers exhausted ({len(chain)} candidates attempted):\n{exhausted_summary}"
    logger.error(final_msg)

    if db:
        record_job_diagnostic_event(
            job_id=job.id,
            level="ERROR",
            component="ai_failover",
            event_type="AI_CHAIN_EXHAUSTED",
            message="All configured AI provider candidates exhausted without success",
            metadata={"failures": failures_log},
            db=db,
        )

    raise AIChainExhaustedError(
        message=final_msg,
        failures=failures_log,
        operation=operation,
    )
