import hashlib
import logging
import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from herald.ai.factory import get_ai_provider, get_research_provider
from herald.config import settings
from herald.core.models import HeraldRequest, HeraldResponse
from herald.db.models import JobState, PodcastJob, RequestMode, SourceType
from herald.db.state_machine import transition_job_state
from herald.extraction.source_cleaner import clean_source_text, deduplicate_source_blocks
from herald.extraction.url_extractor import (
    ArticleExtractionError,
    BlockReason,
    DNSResolutionError,
    SourceAccessBlockedError,
    SSRFVulnerabilityError,
    extract_article_from_url,
)
from herald.gemini.client import (
    GeminiError,
    audit_research_script,
    audit_script_fidelity,
    generate_grounded_research,
    generate_podcast_script,
    normalize_research_dossier,
    repair_research_script,
    repair_script_fidelity,
)
from herald.literal.script_generator import generate_literal_script
from herald.services.diagnostic_recorder import record_job_diagnostic_event
from herald.services.eta_calculator import calculate_script_duration
from herald.services.performance_metrics import record_stage_metric
from herald.services.redaction import sanitize_error
from herald.services.settings_fingerprint import (
    build_generation_settings_snapshot,
    get_job_generation_settings,
)

logger = logging.getLogger("herald.core.pipeline")


def compute_content_hash(text: str, url: str | None = None) -> str:
    """Deterministic hash computed over canonicalized text and optional URL."""
    hasher = hashlib.sha256()
    if url:
        hasher.update(url.strip().lower().encode("utf-8"))
    if text:
        norm = " ".join(text.split())
        hasher.update(norm.encode("utf-8"))
    return hasher.hexdigest()


def _resolve_response_title(
    job: PodcastJob | None, custom_title: str | None = None, script_obj: dict | None = None
) -> str:
    """
    Resolve authoritative display title for HeraldResponse:
    custom_title (request or job) -> (script_json or {}).get("episode_title") -> "Herald Episode"
    """
    c_title = (custom_title or (job.custom_title if job else None) or "").strip()
    if c_title:
        return c_title
    s = script_obj if script_obj is not None else ((job.script_json or {}) if job else {})
    if isinstance(s, dict):
        ep_t = (s.get("episode_title") or "").strip()
        if ep_t:
            return ep_t
    return "Herald Episode"
 

def find_prior_content_candidate(
    db: Session,
    source_hash: str,
    source_url: str | None = None,
    exclude_job_id: str | None = None,
) -> PodcastJob | None:
    """
    Deterministically find the most relevant prior job matching content.
    Hierarchy:
      Tier 1: Active jobs (in-flight)
      Tier 2: COMPLETE jobs
      Tier 3: CANCELLED jobs
      Tier 4: FAILED jobs
      Tie-breaker: created_at DESC (most recent first)
    """
    candidate_filter = and_(
        PodcastJob.source_hash == source_hash,
        PodcastJob.source_text.isnot(None),
        PodcastJob.source_text != "",
        or_(PodcastJob.failed_stage.is_(None), PodcastJob.failed_stage != "EXTRACTION"),
    )

    query = db.query(PodcastJob).filter(candidate_filter)
    if exclude_job_id:
        query = query.filter(PodcastJob.id != exclude_job_id)

    candidates = query.all()
    if not candidates:
        return None

    active_states = {
        JobState.RECEIVED.value,
        JobState.VALIDATING.value,
        JobState.EXTRACTING.value,
        JobState.SOURCE_READY.value,
        JobState.SCRIPTING.value,
        JobState.SCRIPT_READY.value,
        JobState.AWAITING_APPROVAL.value,
        JobState.AWAITING_RERUN_CONFIRMATION.value,
        JobState.QUEUED_TTS.value,
        JobState.SYNTHESIZING.value,
        JobState.ENCODING.value,
        JobState.AUDIO_READY.value,
        JobState.UPLOADING.value,
        JobState.DELIVERING.value,
    }

    def _tier_rank(j: PodcastJob) -> int:
        if j.status in active_states:
            return 1
        elif j.status == JobState.COMPLETE.value:
            return 2
        elif j.status == JobState.CANCELLED.value:
            return 3
        else:
            return 4

    def _sort_key(j: PodcastJob):
        dt = j.created_at
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        ts = dt.timestamp() if dt else 0.0
        return (_tier_rank(j), -ts)

    sorted_candidates = sorted(candidates, key=_sort_key)
    return sorted_candidates[0]


def process_herald_request(db: Session, req: HeraldRequest) -> HeraldResponse:
    """
    Transport-neutral pipeline entry point.
    Handles extraction, normalization, deduplication, script generation, and queuing.
    """
    raw_mode = (req.request_mode or "").lower().strip()
    if not raw_mode:
        raw_mode = settings.get_default_mode()

    # Map legacy aliases
    if raw_mode == "detailed":
        mode_val = RequestMode.RESEARCH.value
        req.research_depth = req.research_depth or "medium"
    elif raw_mode in [m.value for m in RequestMode]:
        mode_val = raw_mode
    else:
        mode_val = (
            RequestMode.LITERAL.value
            if not settings.is_ai_configured()
            else RequestMode.STANDARD.value
        )

    # Validate AI provider requirement for non-literal modes
    is_ai_mode = mode_val in (
        RequestMode.BRIEF.value,
        RequestMode.STANDARD.value,
        RequestMode.RESEARCH.value,
    )
    if mode_val == RequestMode.RESEARCH.value:
        research_prov = get_research_provider()
        if not research_prov or not research_prov.is_configured():
            r_name = getattr(settings, "RESEARCH_PROVIDER", "gemini")
            return HeraldResponse(
                job_id="",
                status=JobState.FAILED_FINAL.value,
                request_mode=mode_val,
                source_type=SourceType.TEXT.value,
                is_duplicate=False,
                message=(
                    f"Research mode requires a provider capable of Google Search Grounding (configured RESEARCH_PROVIDER='{r_name}'). "
                    "Please configure GEMINI_API_KEY with RESEARCH_PROVIDER=gemini to use Research mode, or request 'standard' or 'brief' mode."
                ),
                error_category="INCOMPATIBLE_PROVIDER_FOR_RESEARCH",
            )
    elif is_ai_mode:
        ai_prov = get_ai_provider()
        if not ai_prov or not ai_prov.is_configured():
            return HeraldResponse(
                job_id="",
                status=JobState.FAILED_FINAL.value,
                request_mode=mode_val,
                source_type=SourceType.TEXT.value,
                is_duplicate=False,
                message=(
                    f"AI provider is not configured. Mode '{mode_val}' requires an AI API key. "
                    "Currently available mode is 'literal'. Configure an AI provider or use 'literal' mode."
                ),
                error_category="AI_PROVIDER_NOT_CONFIGURED",
            )

    # Validate Gemini requirement for script verification
    if req.verify_final_script and not settings.GEMINI_API_KEY:
        return HeraldResponse(
            job_id="",
            status=JobState.FAILED_FINAL.value,
            request_mode=mode_val,
            source_type=SourceType.TEXT.value,
            is_duplicate=False,
            message=(
                "Script verification (`verify_final_script=True` / `/verify` / `/doublecheck`) requires Gemini to be configured with GEMINI_API_KEY."
            ),
            error_category="VERIFY_PROVIDER_NOT_CONFIGURED",
        )

    # 1. Transport-level duplicate check (e.g. Telegram message retry)
    if req.transport == "telegram" and req.transport_message_id and req.delivery_target:
        tg_chat = (
            int(req.delivery_target) if str(req.delivery_target).lstrip("-").isdigit() else None
        )
        tg_msg = int(req.transport_message_id) if str(req.transport_message_id).isdigit() else None
        if tg_chat is not None and tg_msg is not None:
            existing_msg_job = (
                db.query(PodcastJob)
                .filter(
                    PodcastJob.transport == "telegram",
                    PodcastJob.telegram_chat_id == tg_chat,
                    PodcastJob.telegram_message_id == tg_msg,
                )
                .first()
            )
            if existing_msg_job:
                ep_title = _resolve_response_title(existing_msg_job)
                return HeraldResponse(
                    job_id=existing_msg_job.id,
                    status=existing_msg_job.status,
                    request_mode=existing_msg_job.request_mode,
                    source_type=existing_msg_job.source_type,
                    is_duplicate=True,
                    message="Telegram message has already been received.",
                    episode_title=ep_title,
                )

    # 2. Extract URL or normalize text
    source_type = SourceType.URL.value if req.source_url else SourceType.TEXT.value
    extracted_text = ""
    source_url = None
    canonical_title = None
    job: PodcastJob | None = None

    telegram_chat = (
        int(req.delivery_target)
        if req.transport == "telegram"
        and req.delivery_target
        and str(req.delivery_target).lstrip("-").isdigit()
        else None
    )
    telegram_msg = (
        int(req.transport_message_id)
        if req.transport == "telegram"
        and req.transport_message_id
        and str(req.transport_message_id).isdigit()
        else None
    )
    telegram_user = (
        int(str(req.requester_identity).replace("telegram:", ""))
        if req.transport == "telegram"
        and str(req.requester_identity).replace("telegram:", "").isdigit()
        else None
    )

    if req.source_url and req.source_url.strip():
        source_url = req.source_url.strip()
        provisional_hash = compute_content_hash("", source_url)
        job_id = str(uuid.uuid4())

        job = PodcastJob(
            id=job_id,
            transport=req.transport,
            telegram_chat_id=telegram_chat,
            telegram_message_id=telegram_msg,
            telegram_user_id=telegram_user,
            sender_email=req.requester_identity if req.transport != "telegram" else None,
            request_mode=mode_val,
            research_depth=req.research_depth,
            source_type=SourceType.URL.value,
            source_url=source_url,
            source_hash=provisional_hash,
            source_text="",
            custom_voice=req.custom_voice,
            custom_speed=req.custom_speed,
            custom_title=req.custom_title,
            tts_chunk_chars=req.tts_chunk_chars or 500,
            verify_final_script=req.verify_final_script,
            status=JobState.EXTRACTING.value,
        )
        try:
            db.add(job)
            db.commit()
            db.refresh(job)
        except Exception as e:
            db.rollback()
            if req.transport == "telegram" and telegram_chat and telegram_msg:
                existing = (
                    db.query(PodcastJob)
                    .filter(
                        PodcastJob.transport == "telegram",
                        PodcastJob.telegram_chat_id == telegram_chat,
                        PodcastJob.telegram_message_id == telegram_msg,
                    )
                    .first()
                )
                if existing:
                    ep_title = _resolve_response_title(existing)
                    return HeraldResponse(
                        job_id=existing.id,
                        status=existing.status,
                        request_mode=existing.request_mode,
                        source_type=existing.source_type,
                        is_duplicate=True,
                        rerun_of_job_id=existing.rerun_of_job_id,
                        message="Telegram message already accepted.",
                        episode_title=ep_title,
                    )
            raise e

        try:
            art_title, art_text, canon_url = extract_article_from_url(source_url)
            canonical_title = art_title
            source_url = canon_url
            extracted_text = f"Title: {art_title}\n\n{art_text}" if art_title else art_text
            record_stage_metric(
                job_id=job.id,
                stage="URL_EXTRACTION",
                status="SUCCESS",
                started_at=datetime.now(UTC),
                metadata_json={"extraction_method": "DIRECT_HTTP", "url": source_url},
            )
            record_job_diagnostic_event(
                job.id,
                "INFO",
                "extraction",
                "EXTRACTION_SUCCESS",
                f"Direct HTTP URL extraction succeeded ({len(art_text)} chars).",
                metadata={"extraction_method": "DIRECT_HTTP", "url": source_url, "chars": len(art_text)},
                db=db,
            )
        except SSRFVulnerabilityError as e:
            error_cat = getattr(e, "error_category", "SSRF_PROTECTION")
            _, safe_msg = sanitize_error(e)
            try:
                from herald.services.failure_diagnostics import collect_failure_diagnostics
                collect_failure_diagnostics(stage="extraction", error=e, target_url=source_url, job_id=job.id, db=db)
            except Exception as diag_err:
                logger.warning(f"Failure diagnostics capture error: {diag_err}")
            record_stage_metric(
                job_id=job.id, stage="URL_EXTRACTION", status="FAILED",
                started_at=datetime.now(UTC), metadata_json={"error_category": error_cat, "url": source_url, "direct_error": safe_msg},
            )
            record_job_diagnostic_event(
                job.id, "ERROR", "extraction", "EXTRACTION_FAILED",
                f"SSRF security violation for URL: {safe_msg}",
                metadata={"error_category": error_cat, "url": source_url, "direct_error": safe_msg}, db=db,
            )
            transition_job_state(
                db, job, JobState.FAILED_FINAL.value,
                component="herald-core", message=f"Security violation: {safe_msg}",
                error_category=error_cat, commit=False,
            )
            job.failed_stage = "EXTRACTION"
            job.error_code = error_cat
            job.error_detail = safe_msg
            db.commit()
            try:
                from herald.services.diagnostics_export import ensure_terminal_diagnostics_archive
                ensure_terminal_diagnostics_archive(job.id, JobState.FAILED_FINAL.value)
            except Exception as arc_err:
                logger.warning("Failed ensuring terminal diagnostics archive: %s", arc_err)
            return HeraldResponse(
                job_id=job.id,
                status=JobState.FAILED_FINAL.value,
                request_mode=mode_val,
                source_type=SourceType.URL.value,
                is_duplicate=False,
                message=f"Security violation: {safe_msg}",
                error_category=error_cat,
            )
        except DNSResolutionError as e:
            error_cat = getattr(e, "error_category", "DNS_RESOLUTION_ERROR")
            _, safe_msg = sanitize_error(e)
            try:
                from herald.services.failure_diagnostics import collect_failure_diagnostics
                collect_failure_diagnostics(stage="extraction", error=e, target_url=source_url, job_id=job.id, db=db)
            except Exception as diag_err:
                logger.warning(f"Failure diagnostics capture error: {diag_err}")
            record_stage_metric(
                job_id=job.id, stage="URL_EXTRACTION", status="FAILED",
                started_at=datetime.now(UTC), metadata_json={"error_category": error_cat, "url": source_url, "direct_error": safe_msg},
            )
            record_job_diagnostic_event(
                job.id, "ERROR", "extraction", "EXTRACTION_FAILED",
                f"DNS resolution failed for URL: {safe_msg}",
                metadata={"error_category": error_cat, "url": source_url, "direct_error": safe_msg}, db=db,
            )
            transition_job_state(
                db, job, JobState.FAILED_FINAL.value,
                component="herald-core", message=f"DNS resolution failed: {safe_msg}",
                error_category=error_cat, commit=False,
            )
            job.failed_stage = "EXTRACTION"
            job.error_code = error_cat
            job.error_detail = safe_msg
            db.commit()
            try:
                from herald.services.diagnostics_export import ensure_terminal_diagnostics_archive
                ensure_terminal_diagnostics_archive(job.id, JobState.FAILED_FINAL.value)
            except Exception as arc_err:
                logger.warning("Failed ensuring terminal diagnostics archive: %s", arc_err)
            return HeraldResponse(
                job_id=job.id,
                status=JobState.FAILED_FINAL.value,
                request_mode=mode_val,
                source_type=SourceType.URL.value,
                is_duplicate=False,
                message=f"URL retrieval failed: {safe_msg}",
                error_category=error_cat,
            )
        except SourceAccessBlockedError as e:
            error_cat = getattr(e, "error_category", "SOURCE_ACCESS_BLOCKED")
            block_reason = getattr(e, "block_reason", BlockReason.PUBLIC_RETRIEVAL_BLOCK)
            _, safe_msg = sanitize_error(e)
            url_context_attempted = False
            fallback_result = "NOT_ATTEMPTED"

            # Gemini URL Context may ONLY be attempted for public retrieval blocks or rate limits,
            # never for 401/auth, paywalls, captchas, interstitials, SSRF, or Literal mode.
            is_eligible_block = block_reason in (
                BlockReason.PUBLIC_RETRIEVAL_BLOCK,
                BlockReason.RATE_LIMITED,
            )
            can_attempt_fallback = (
                is_eligible_block
                and mode_val != "literal"
                and settings.is_ai_configured()
                and (settings.AI_PROVIDER or "").lower().strip() == "gemini"
                and source_url
            )
            if can_attempt_fallback:
                url_context_attempted = True
                try:
                    from herald.gemini.client import extract_article_via_url_context
                    logger.info(f"Attempting Gemini URL Context fallback for blocked URL ({block_reason}): {source_url}")
                    record_job_diagnostic_event(
                        job.id, "INFO", "extraction", "URL_CONTEXT_FALLBACK_ATTEMPT",
                        f"Source access blocked ({block_reason}); attempting Gemini URL Context fallback.",
                        metadata={"url": source_url, "original_error": error_cat, "block_reason": block_reason}, db=db,
                    )
                    url_ctx_result = extract_article_via_url_context(
                        url=source_url, api_key=None, model_name=None, job_id=job.id,
                    )
                    if url_ctx_result and url_ctx_result.get("body", "").strip():
                        # URL Context succeeded — use extracted content
                        ctx_title = url_ctx_result.get("title", "").strip()
                        ctx_body = url_ctx_result["body"].strip()
                        canonical_title = ctx_title or canonical_title
                        extracted_text = f"Title: {ctx_title}\n\n{ctx_body}" if ctx_title else ctx_body
                        fallback_result = "SUCCESS"
                        record_stage_metric(
                            job_id=job.id,
                            stage="URL_EXTRACTION",
                            status="SUCCESS",
                            started_at=datetime.now(UTC),
                            metadata_json={
                                "extraction_method": "GEMINI_URL_CONTEXT",
                                "direct_error_category": error_cat,
                                "direct_error": safe_msg,
                                "block_reason": block_reason,
                                "fallback_attempted": True,
                                "fallback_result": "SUCCESS",
                                "url": source_url,
                            },
                        )
                        record_job_diagnostic_event(
                            job.id, "INFO", "extraction", "URL_CONTEXT_FALLBACK_SUCCESS",
                            f"Gemini URL Context fallback succeeded ({len(ctx_body)} chars).",
                            metadata={"url": source_url, "title": ctx_title, "body_chars": len(ctx_body)}, db=db,
                        )
                        record_job_diagnostic_event(
                            job.id, "INFO", "extraction", "EXTRACTION_SUCCESS",
                            f"Gemini URL Context extraction succeeded ({len(ctx_body)} chars).",
                            metadata={
                                "extraction_method": "GEMINI_URL_CONTEXT",
                                "direct_error_category": error_cat,
                                "direct_error": safe_msg,
                                "block_reason": block_reason,
                                "fallback_attempted": True,
                                "fallback_result": "SUCCESS",
                                "url": source_url,
                                "chars": len(ctx_body),
                            },
                            db=db,
                        )
                        logger.info(f"URL Context fallback succeeded for {source_url}: {len(ctx_body)} chars")
                    else:
                        fallback_result = "FAILED"
                except Exception as ctx_err:
                    fallback_result = "FAILED"
                    _, safe_ctx_msg = sanitize_error(ctx_err)
                    logger.warning(f"URL Context fallback failed for {source_url}: {ctx_err}")
                    record_job_diagnostic_event(
                        job.id, "WARNING", "extraction", "URL_CONTEXT_FALLBACK_FAILED",
                        f"Gemini URL Context fallback failed: {safe_ctx_msg}",
                        metadata={"url": source_url}, db=db,
                    )

            if fallback_result != "SUCCESS":
                # Fallback not attempted or not successful — fail the job
                try:
                    from herald.services.failure_diagnostics import collect_failure_diagnostics
                    collect_failure_diagnostics(stage="extraction", error=e, target_url=source_url, job_id=job.id, db=db)
                except Exception as diag_err:
                    logger.warning(f"Failure diagnostics capture error: {diag_err}")
                record_stage_metric(
                    job_id=job.id, stage="URL_EXTRACTION", status="FAILED",
                    started_at=datetime.now(UTC),
                    metadata_json={
                        "extraction_method": "DIRECT_HTTP",
                        "direct_error_category": error_cat,
                        "direct_error": safe_msg,
                        "block_reason": block_reason,
                        "fallback_attempted": url_context_attempted,
                        "fallback_result": fallback_result,
                        "url": source_url,
                    },
                )
                record_job_diagnostic_event(
                    job.id, "ERROR", "extraction", "EXTRACTION_FAILED",
                    f"Source access blocked: {safe_msg}",
                    metadata={
                        "extraction_method": "DIRECT_HTTP",
                        "direct_error_category": error_cat,
                        "direct_error": safe_msg,
                        "block_reason": block_reason,
                        "fallback_attempted": url_context_attempted,
                        "fallback_result": fallback_result,
                        "url": source_url,
                    },
                    db=db,
                )
                if mode_val == "literal":
                    user_message = (
                        "URL extraction blocked: Literal mode does not use AI-assisted URL retrieval. "
                        "Please paste the article text directly into Herald."
                    )
                elif url_context_attempted:
                    user_message = (
                        "URL extraction failed: Herald could not retrieve the original public page. "
                        "Please paste the article text directly into Herald."
                    )
                else:
                    user_message = (
                        f"URL extraction failed: Access blocked ({block_reason.lower().replace('_', ' ')}). "
                        "Please paste the article text directly into Herald."
                    )
                transition_job_state(
                    db, job, JobState.FAILED_FINAL.value,
                    component="herald-core", message=user_message,
                    error_category=error_cat, commit=False,
                )
                job.failed_stage = "EXTRACTION"
                job.error_code = error_cat
                job.error_detail = user_message
                db.commit()
                try:
                    from herald.services.diagnostics_export import ensure_terminal_diagnostics_archive
                    ensure_terminal_diagnostics_archive(job.id, JobState.FAILED_FINAL.value)
                except Exception as arc_err:
                    logger.warning("Failed ensuring terminal diagnostics archive: %s", arc_err)
                return HeraldResponse(
                    job_id=job.id,
                    status=JobState.FAILED_FINAL.value,
                    request_mode=mode_val,
                    source_type=SourceType.URL.value,
                    is_duplicate=False,
                    message=user_message,
                    error_category=error_cat,
                )
        except ArticleExtractionError as e:
            error_cat = getattr(e, "error_category", "EXTRACTION_FAILURE")
            _, safe_msg = sanitize_error(e)
            try:
                from herald.services.failure_diagnostics import collect_failure_diagnostics
                collect_failure_diagnostics(stage="extraction", error=e, target_url=source_url, job_id=job.id, db=db)
            except Exception as diag_err:
                logger.warning(f"Failure diagnostics capture error: {diag_err}")
            record_stage_metric(
                job_id=job.id, stage="URL_EXTRACTION", status="FAILED",
                started_at=datetime.now(UTC), metadata_json={"error_category": error_cat, "url": source_url, "direct_error": safe_msg},
            )
            record_job_diagnostic_event(
                job.id, "ERROR", "extraction", "EXTRACTION_FAILED",
                f"Article extraction failed: {safe_msg}",
                metadata={"error_category": error_cat, "url": source_url, "direct_error": safe_msg}, db=db,
            )
            transition_job_state(
                db, job, JobState.FAILED_FINAL.value,
                component="herald-core", message=f"Article extraction failed: {safe_msg}",
                error_category=error_cat, commit=False,
            )
            job.failed_stage = "EXTRACTION"
            job.error_code = error_cat
            job.error_detail = safe_msg
            db.commit()
            try:
                from herald.services.diagnostics_export import ensure_terminal_diagnostics_archive
                ensure_terminal_diagnostics_archive(job.id, JobState.FAILED_FINAL.value)
            except Exception as arc_err:
                logger.warning("Failed ensuring terminal diagnostics archive: %s", arc_err)
            return HeraldResponse(
                job_id=job.id,
                status=JobState.FAILED_FINAL.value,
                request_mode=mode_val,
                source_type=SourceType.URL.value,
                is_duplicate=False,
                message=f"URL extraction failed: {safe_msg}",
                error_category=error_cat,
            )
        except Exception as e:
            _, safe_msg = sanitize_error(e)
            try:
                from herald.services.failure_diagnostics import collect_failure_diagnostics
                collect_failure_diagnostics(stage="extraction", error=e, target_url=source_url, job_id=job.id, db=db)
            except Exception as diag_err:
                logger.warning(f"Failure diagnostics capture error: {diag_err}")
            record_stage_metric(
                job_id=job.id, stage="URL_EXTRACTION", status="FAILED",
                started_at=datetime.now(UTC), metadata_json={"error_category": "EXTRACTION_FAILURE", "url": source_url, "direct_error": safe_msg},
            )
            record_job_diagnostic_event(
                job.id, "ERROR", "extraction", "EXTRACTION_FAILED",
                f"Unexpected extraction error: {safe_msg}",
                metadata={"error_category": "EXTRACTION_FAILURE", "url": source_url, "direct_error": safe_msg}, db=db,
            )
            transition_job_state(
                db, job, JobState.FAILED_FINAL.value,
                component="herald-core", message=f"Extraction failed: {safe_msg}",
                error_category="EXTRACTION_FAILURE", commit=False,
            )
            job.failed_stage = "EXTRACTION"
            job.error_code = "EXTRACTION_FAILURE"
            job.error_detail = safe_msg
            db.commit()
            try:
                from herald.services.diagnostics_export import ensure_terminal_diagnostics_archive
                ensure_terminal_diagnostics_archive(job.id, JobState.FAILED_FINAL.value)
            except Exception as arc_err:
                logger.warning("Failed ensuring terminal diagnostics archive: %s", arc_err)
            return HeraldResponse(
                job_id=job.id,
                status=JobState.FAILED_FINAL.value,
                request_mode=mode_val,
                source_type=SourceType.URL.value,
                is_duplicate=False,
                message=f"Extraction failed: {safe_msg}",
                error_category="EXTRACTION_FAILURE",
            )
    else:
        extracted_text = req.source_text or ""

    if not extracted_text.strip():
        if job:
            job.status = JobState.FAILED_FINAL.value
            job.failed_stage = "EXTRACTION"
            job.error_code = "EMPTY_SOURCE"
            job.error_detail = "No usable source text or valid URL was provided."
            db.commit()
            try:
                from herald.services.diagnostics_export import ensure_terminal_diagnostics_archive
                ensure_terminal_diagnostics_archive(job.id, JobState.FAILED_FINAL.value)
            except Exception as arc_err:
                logger.warning("Failed ensuring terminal diagnostics archive: %s", arc_err)
            return HeraldResponse(
                job_id=job.id,
                status=JobState.FAILED_FINAL.value,
                request_mode=mode_val,
                source_type=source_type,
                is_duplicate=False,
                message="No usable source text or valid URL was provided.",
                error_category="EMPTY_SOURCE",
            )
        return HeraldResponse(
            job_id="",
            status=JobState.FAILED_FINAL.value,
            request_mode=mode_val,
            source_type=source_type,
            is_duplicate=False,
            message="No usable source text or valid URL was provided.",
            error_category="EMPTY_SOURCE",
        )

    # Clean and deduplicate text blocks
    cleaned = clean_source_text(extracted_text)
    deduped_text, _ = deduplicate_source_blocks(cleaned)
    source_hash = compute_content_hash(deduped_text, source_url)

    # Resolve title if not explicitly provided
    resolved_title = (req.custom_title or canonical_title or "").strip()
    if not resolved_title and deduped_text:
        from herald.literal.script_generator import extract_title_and_body

        ext_title, _ = extract_title_and_body(deduped_text)
        if ext_title and ext_title != "Herald Episode":
            resolved_title = ext_title

    # 3. Settings snapshot and content candidate lookup
    settings_snapshot = build_generation_settings_snapshot(
        mode=mode_val,
        research_depth=req.research_depth,
        voice=req.custom_voice,
        speed=req.custom_speed,
        custom_title=resolved_title,
        chunk_chars=req.tts_chunk_chars,
        verify=req.verify_final_script,
    )
    prior_job = find_prior_content_candidate(
        db,
        source_hash=source_hash,
        source_url=source_url,
        exclude_job_id=job.id if job else None,
    )

    # 4. Finalize PodcastJob (every intentional request gets its own immutable record)
    if job is not None:
        job.source_url = source_url
        job.source_hash = source_hash
        job.source_text = deduped_text
        job.custom_title = resolved_title
        job.rerun_of_job_id = prior_job.id if prior_job else None
        job.generation_settings_json = settings_snapshot
        job.status = JobState.RECEIVED.value
        db.commit()
        db.refresh(job)
    else:
        job_id = str(uuid.uuid4())
        job = PodcastJob(
            id=job_id,
            transport=req.transport,
            telegram_chat_id=telegram_chat,
            telegram_message_id=telegram_msg,
            telegram_user_id=telegram_user,
            sender_email=req.requester_identity if req.transport != "telegram" else None,
            request_mode=mode_val,
            research_depth=req.research_depth,
            source_type=source_type,
            source_url=source_url,
            source_hash=source_hash,
            source_text=deduped_text,
            custom_voice=req.custom_voice,
            custom_speed=req.custom_speed,
            custom_title=resolved_title,
            tts_chunk_chars=req.tts_chunk_chars or 500,
            verify_final_script=req.verify_final_script,
            rerun_of_job_id=prior_job.id if prior_job else None,
            generation_settings_json=settings_snapshot,
            status=JobState.RECEIVED.value,
        )

        try:
            db.add(job)
            db.commit()
            db.refresh(job)
        except Exception as e:
            db.rollback()
            # Handle concurrent race if another process created this Telegram job
            if req.transport == "telegram" and telegram_chat and telegram_msg:
                existing = (
                    db.query(PodcastJob)
                    .filter(
                        PodcastJob.transport == "telegram",
                        PodcastJob.telegram_chat_id == telegram_chat,
                        PodcastJob.telegram_message_id == telegram_msg,
                    )
                    .first()
                )
                if existing:
                    ep_title = _resolve_response_title(existing)
                    return HeraldResponse(
                        job_id=existing.id,
                        status=existing.status,
                        request_mode=existing.request_mode,
                        source_type=existing.source_type,
                        is_duplicate=True,
                        rerun_of_job_id=existing.rerun_of_job_id,
                        message="Telegram message already accepted.",
                        episode_title=ep_title,
                    )
            raise e

    # Case D: Duplicate content + hold_for_approval == False
    # Prompt the user for rerun confirmation BEFORE running any scripting / AI calls.
    if prior_job is not None and not req.hold_for_approval:
        transition_job_state(db, job, JobState.VALIDATING.value, component="herald-core")
        transition_job_state(db, job, JobState.SOURCE_READY.value, component="herald-core")
        transition_job_state(
            db, job, JobState.AWAITING_RERUN_CONFIRMATION.value, component="herald-core"
        )
        record_job_diagnostic_event(
            job.id,
            "INFO",
            "intake",
            "AWAITING_RERUN_CONFIRMATION",
            f"Prior content match found (job {prior_job.id}, status {prior_job.status}); awaiting user rerun confirmation before scripting.",
            metadata={"prior_job_id": prior_job.id, "prior_status": prior_job.status},
            db=db,
        )
        db.commit()
        ep_title = _resolve_response_title(job, custom_title=job.custom_title)
        return HeraldResponse(
            job_id=job.id,
            status=job.status,
            request_mode=job.request_mode,
            source_type=job.source_type,
            is_duplicate=True,
            rerun_of_job_id=prior_job.id,
            message="Prior generation found; confirmation required before synthesis.",
            episode_title=ep_title,
        )

    # Cases A, B, C: Proceed directly to script generation
    return execute_script_generation(
        db,
        job,
        hold_for_approval=req.hold_for_approval,
        is_duplicate=(prior_job is not None),
        rerun_of_job_id=prior_job.id if prior_job else None,
    )


def execute_script_generation(
    db: Session,
    job: PodcastJob,
    hold_for_approval: bool,
    is_duplicate: bool = False,
    rerun_of_job_id: str | None = None,
) -> HeraldResponse:
    """
    Execute script generation, verification, and transition to either AWAITING_APPROVAL or QUEUED_TTS.
    Can be called for initial jobs (Cases A, B, C) or upon approving a rerun in AWAITING_RERUN_CONFIRMATION (Case D).
    """
    mode_val = job.request_mode
    source_url = job.source_url

    if job.status == JobState.RECEIVED.value:
        transition_job_state(db, job, JobState.VALIDATING.value, component="herald-core")
        transition_job_state(db, job, JobState.SOURCE_READY.value, component="herald-core")
        record_job_diagnostic_event(
            job.id,
            "INFO",
            "intake",
            "INTAKE_RECEIVED",
            f"Accepted {job.transport} intake request (mode={mode_val})",
            db=db,
        )
        if source_url:
            record_job_diagnostic_event(
                job.id,
                "INFO",
                "extraction",
                "EXTRACTION_COMPLETE",
                f"Extracted article from source URL ({len(job.source_text or '')} chars)",
                metadata={"source_url": source_url, "char_count": len(job.source_text or "")},
                db=db,
            )

    transition_job_state(db, job, JobState.SCRIPTING.value, component="herald-core")
    record_job_diagnostic_event(
        job.id,
        "INFO",
        "scripting",
        "SCRIPTING_BEGIN",
        f"Starting script generation for mode '{mode_val}'",
        db=db,
    )

    active_ai_provider: str | None = None
    active_ai_model: str | None = None
    active_operation: str | None = None

    try:
        if mode_val == RequestMode.LITERAL.value:
            active_operation = "literal_script"
            logger.info(f"Generating Literal script for job '{job.id}' (zero AI requests)")
            t_script0 = datetime.now(UTC)
            script_resp = generate_literal_script(
                source_text=job.source_text,
                source_title=job.custom_title,
                max_segment_chars=job.tts_chunk_chars or 1000,
            )
            job.script_json = script_resp.model_dump()
            db.commit()
            record_stage_metric(
                job_id=job.id,
                stage="LITERAL_SCRIPT",
                started_at=t_script0,
                finished_at=datetime.now(UTC),
                status="success",
                input_chars=len(job.source_text or ""),
            )
        elif mode_val == RequestMode.RESEARCH.value:
            active_ai_provider = getattr(settings, "RESEARCH_PROVIDER", "gemini")
            active_ai_model = getattr(settings, "GEMINI_RESEARCH_MODEL", "gemini-3.6-flash")
            # Multi-stage grounded research workflow
            if not job.research_grounding_json:
                active_operation = "grounded_research"
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "research",
                    "RESEARCH_GROUNDING_BEGIN",
                    f"Starting grounded research (depth={job.research_depth or 'medium'})",
                    db=db,
                )
                grounded_data = generate_grounded_research(
                    source_text=job.source_text,
                    research_depth=job.research_depth or "medium",
                    job_id=job.id,
                )
                job.research_grounding_json = grounded_data
                job.research_search_count = grounded_data.get("search_count", 0)
                job.research_source_count = grounded_data.get("source_count", 0)
                db.commit()
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "research",
                    "RESEARCH_GROUNDING_COMPLETE",
                    f"Grounded research complete ({job.research_source_count} sources, {job.research_search_count} searches)",
                    metadata={
                        "sources_count": job.research_source_count,
                        "search_count": job.research_search_count,
                    },
                    db=db,
                )

            if not job.research_json:
                active_operation = "research_normalization"
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "research",
                    "RESEARCH_NORMALIZATION_BEGIN",
                    "Normalizing research claims and sources into structured dossier",
                    db=db,
                )
                dossier = normalize_research_dossier(
                    source_text=job.source_text,
                    grounded_research_data=job.research_grounding_json,
                    job_id=job.id,
                )
                job.research_json = dossier.model_dump()
                job.research_model = settings.GEMINI_RESEARCH_MODEL
                db.commit()
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "research",
                    "RESEARCH_NORMALIZATION_COMPLETE",
                    "Research dossier normalized successfully",
                    db=db,
                )

            if not job.script_json:
                active_operation = "research_script"
                script = generate_podcast_script(
                    source_text=job.source_text,
                    request_mode="research",
                    research_dossier=job.research_json,
                    source_title=job.custom_title,
                    job_id=job.id,
                )
                job.script_json = script.model_dump()
                db.commit()

            if not job.research_audit_json:
                active_operation = "research_audit"
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "research",
                    "RESEARCH_AUDIT_BEGIN",
                    "Auditing research script against grounding sources",
                    db=db,
                )
                audit = audit_research_script(
                    source_text=job.source_text,
                    research_dossier=job.research_json,
                    script_dict=job.script_json,
                    job_id=job.id,
                )
                job.research_audit_json = audit.model_dump()
                db.commit()
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "research",
                    "RESEARCH_AUDIT_COMPLETE",
                    f"Research audit completed (has_material_issues={bool((job.research_audit_json or {}).get('has_material_issues'))})",
                    metadata={
                        "has_material_issues": bool(
                            (job.research_audit_json or {}).get("has_material_issues")
                        )
                    },
                    db=db,
                )

            audit_data = job.research_audit_json or {}
            if audit_data.get("has_material_issues") and job.research_repair_count == 0:
                active_operation = "research_repair"
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "research",
                    "SCRIPT_REPAIR_BEGIN",
                    "Repairing research script based on audit findings",
                    db=db,
                )
                repaired = repair_research_script(
                    source_text=job.source_text,
                    research_dossier=job.research_json,
                    script_dict=job.script_json,
                    audit_result=audit_data,
                    job_id=job.id,
                )
                job.script_json = repaired.model_dump()
                job.research_repair_count = 1
                db.commit()
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "research",
                    "SCRIPT_REPAIR_COMPLETE",
                    "Research script repair completed",
                    db=db,
                )
        else:
            # Brief or Standard AI mode
            active_operation = "standard_script"
            t_script0 = datetime.now(UTC)
            provider = get_ai_provider()
            if not provider:
                raise GeminiError("AI provider is not configured.")
            active_ai_provider = getattr(provider, "name", settings.AI_PROVIDER)
            m_val = (
                getattr(provider, "configured_model", None)
                or getattr(provider, "model_name", None)
            )
            active_ai_model = (
                m_val if isinstance(m_val, str) and m_val
                else (settings.GEMINI_MODEL if active_ai_provider == "gemini" else "")
            )
            script_resp = provider.generate_script(
                source_text=job.source_text,
                request_mode=mode_val,
                source_title=job.custom_title,
                job_id=job.id,
            )
            job.script_json = script_resp.model_dump()
            job.gemini_model = active_ai_model or settings.GEMINI_MODEL
            db.commit()
            record_stage_metric(
                job_id=job.id,
                stage="AI_SCRIPT",
                started_at=t_script0,
                finished_at=datetime.now(UTC),
                status="success",
                input_chars=len(job.source_text or ""),
            )

        # Fidelity verification for non-research modes when verify_final_script=True
        if mode_val != RequestMode.RESEARCH.value and job.verify_final_script:
            if not job.verify_audit_json:
                active_operation = "verification"
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "verification",
                    "VERIFY_AUDIT_BEGIN",
                    "Starting script fidelity audit against source text",
                    db=db,
                )
                try:
                    v_audit = audit_script_fidelity(
                        source_text=job.source_text,
                        script_dict=job.script_json,
                        job_id=job.id,
                    )
                    job.verify_audit_json = v_audit.model_dump()
                    db.commit()
                    record_job_diagnostic_event(
                        job.id,
                        "INFO",
                        "verification",
                        "VERIFY_AUDIT_COMPLETE",
                        f"Script fidelity audit complete (has_material_issues={bool(v_audit.has_material_issues)})",
                        metadata={"has_material_issues": bool(v_audit.has_material_issues)},
                        db=db,
                    )
                except Exception as ve:
                    logger.warning(f"Fidelity audit failed for job '{job.id}': {ve}")
                    record_job_diagnostic_event(
                        job.id,
                        "WARNING",
                        "verification",
                        "VERIFY_AUDIT_FAILED",
                        f"Fidelity audit failed non-fatally: {ve}",
                        db=db,
                    )

            v_data = job.verify_audit_json or {}
            if v_data.get("has_material_issues") and (job.verify_repair_count or 0) == 0:
                active_operation = "verification_repair"
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "verification",
                    "VERIFY_REPAIR_BEGIN",
                    "Repairing script based on fidelity audit findings",
                    db=db,
                )
                try:
                    repaired_v = repair_script_fidelity(
                        source_text=job.source_text,
                        script_dict=job.script_json,
                        audit_result=v_data,
                        job_id=job.id,
                    )
                    job.script_json = repaired_v.model_dump()
                    job.verify_repair_count = 1
                    db.commit()
                    record_job_diagnostic_event(
                        job.id,
                        "INFO",
                        "verification",
                        "VERIFY_REPAIR_COMPLETE",
                        "Script fidelity repair completed",
                        db=db,
                    )
                except Exception as re_err:
                    logger.warning(f"Fidelity repair failed for job '{job.id}': {re_err}")
                    record_job_diagnostic_event(
                        job.id,
                        "WARNING",
                        "verification",
                        "VERIFY_REPAIR_FAILED",
                        f"Fidelity repair failed non-fatally: {re_err}",
                        db=db,
                    )

        transition_job_state(db, job, JobState.SCRIPT_READY.value, component="herald-core")
        record_job_diagnostic_event(
            job.id,
            "INFO",
            "scripting",
            "SCRIPTING_COMPLETE",
            "Script generated successfully",
            metadata={"segments_count": len((job.script_json or {}).get("segments", []))},
            db=db,
        )

        script_obj = job.script_json or {}
        ep_title = _resolve_response_title(
            job, custom_title=job.custom_title, script_obj=script_obj
        )
        dur_info = calculate_script_duration(
            script_obj, job.custom_speed or settings.KOKORO_SPEED
        )

        if hold_for_approval:
            job.approval_required = True
            job.approval_requested_at = None
            job.telegram_approval_message_id = None
            transition_job_state(
                db, job, JobState.AWAITING_APPROVAL.value, component="herald-core"
            )
            record_job_diagnostic_event(
                job.id, "INFO", "approval", "APPROVAL_REQUESTED", "Job held for user approval", db=db
            )
            db.commit()
            return HeraldResponse(
                job_id=job.id,
                status=job.status,
                request_mode=job.request_mode,
                source_type=job.source_type,
                is_duplicate=is_duplicate,
                rerun_of_job_id=rerun_of_job_id,
                message="Script ready and awaiting approval.",
                episode_title=ep_title,
                estimated_minutes=dur_info.get("estimated_minutes"),
            )

        transition_job_state(db, job, JobState.QUEUED_TTS.value, component="herald-core")
        record_job_diagnostic_event(
            job.id,
            "INFO",
            "queue",
            "QUEUED_FOR_TTS",
            "Job queued for Kokoro TTS synthesis",
            db=db,
        )
        db.commit()

        return HeraldResponse(
            job_id=job.id,
            status=job.status,
            request_mode=job.request_mode,
            source_type=job.source_type,
            is_duplicate=is_duplicate,
            rerun_of_job_id=rerun_of_job_id,
            message="Accepted and queued for TTS synthesis.",
            episode_title=ep_title,
            estimated_minutes=dur_info.get("estimated_minutes"),
        )
    except Exception as e:
        logger.error(f"Script generation failure for job '{job.id}': {e}")
        cat, safe_msg = sanitize_error(e)
        eff_cat = cat if cat and cat != "UNKNOWN_ERROR" else "SCRIPT_GENERATION_FAILED"
        try:
            from herald.services.failure_diagnostics import collect_failure_diagnostics
            collect_failure_diagnostics(
                stage="research" if mode_val == RequestMode.RESEARCH.value else "scripting",
                error=e,
                job_id=job.id,
                attempt=job.attempt_count or 1,
                db=db,
                provider=active_ai_provider,
                model=active_ai_model,
                operation=active_operation,
            )
        except Exception as diag_err:
            logger.warning(f"Failure diagnostics capture error: {diag_err}")
        record_job_diagnostic_event(
            job.id,
            "ERROR",
            "scripting",
            "SCRIPTING_FAILED",
            f"Script generation failed: {safe_msg}",
            metadata={"error_category": eff_cat},
            db=db,
        )
        transition_job_state(
            db,
            job,
            JobState.FAILED_FINAL.value,
            component="herald-core",
            message=safe_msg,
            error_category=eff_cat,
        )
        try:
            from herald.services.diagnostics_export import ensure_terminal_diagnostics_archive
            ensure_terminal_diagnostics_archive(job.id, JobState.FAILED_FINAL.value)
        except Exception as arc_err:
            logger.warning("Failed ensuring terminal diagnostics archive: %s", arc_err)
        return HeraldResponse(
            job_id=job.id,
            status=job.status,
            request_mode=job.request_mode,
            source_type=job.source_type,
            is_duplicate=is_duplicate,
            rerun_of_job_id=rerun_of_job_id,
            message=f"Script generation failed: {safe_msg}",
            error_category=eff_cat,
        )
