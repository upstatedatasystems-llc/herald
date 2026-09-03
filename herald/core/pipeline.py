import hashlib
import logging
import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from herald.ai.factory import get_ai_provider
from herald.config import settings
from herald.core.models import HeraldRequest, HeraldResponse
from herald.db.models import JobState, PodcastJob, RequestMode, SourceType
from herald.db.state_machine import transition_job_state
from herald.extraction.source_cleaner import clean_source_text, deduplicate_source_blocks
from herald.extraction.url_extractor import (
    ArticleExtractionError,
    SourceAccessBlockedError,
    SSRFVulnerabilityError,
    extract_article_from_url,
)
from herald.gemini.client import (
    GeminiError,
    audit_research_script,
    generate_grounded_research,
    generate_podcast_script,
    normalize_research_dossier,
    repair_research_script,
)
from herald.literal.script_generator import generate_literal_script
from herald.services.eta_calculator import calculate_script_duration
from herald.services.performance_metrics import record_stage_metric

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
    if is_ai_mode and not settings.is_ai_configured():
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

    # Research mode is Gemini-only (requires Google Search Grounding).
    if mode_val == RequestMode.RESEARCH.value:
        active_prov = (settings.AI_PROVIDER or "").lower().strip()
        if active_prov != "gemini":
            return HeraldResponse(
                job_id="",
                status=JobState.FAILED_FINAL.value,
                request_mode=mode_val,
                source_type=SourceType.TEXT.value,
                is_duplicate=False,
                message=(
                    f"Research mode is only supported with the Google Gemini provider (active provider: '{settings.AI_PROVIDER}'). "
                    "Please set AI_PROVIDER=gemini to use Research mode, or request 'standard' or 'brief' mode."
                ),
                error_category="INCOMPATIBLE_PROVIDER_FOR_RESEARCH",
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

    if req.source_url and req.source_url.strip():
        source_url = req.source_url.strip()
        try:
            art_title, art_text, canon_url = extract_article_from_url(source_url)
            canonical_title = art_title
            source_url = canon_url
            extracted_text = f"Title: {art_title}\n\n{art_text}" if art_title else art_text
        except SSRFVulnerabilityError as e:
            return HeraldResponse(
                job_id="",
                status=JobState.FAILED_FINAL.value,
                request_mode=mode_val,
                source_type=SourceType.URL.value,
                is_duplicate=False,
                message=f"Security violation: {e}",
                error_category="SSRF_PROTECTION",
            )
        except (ArticleExtractionError, SourceAccessBlockedError) as e:
            return HeraldResponse(
                job_id="",
                status=JobState.FAILED_FINAL.value,
                request_mode=mode_val,
                source_type=SourceType.URL.value,
                is_duplicate=False,
                message=f"URL extraction failed: {e}",
                error_category="EXTRACTION_FAILURE",
            )
    else:
        extracted_text = req.source_text or ""

    if not extracted_text.strip():
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

    # 3. Content deduplication check
    candidate_filter = (
        or_(
            PodcastJob.source_hash == source_hash,
            and_(PodcastJob.source_url.isnot(None), PodcastJob.source_url == source_url),
        )
        if source_url
        else (PodcastJob.source_hash == source_hash)
    )
    existing_candidates = (
        db.query(PodcastJob)
        .filter(candidate_filter)
        .filter(PodcastJob.status != JobState.FAILED_FINAL.value)
        .all()
    )

    req_voice = (req.custom_voice or "").strip()
    req_speed = round(float(req.custom_speed), 2) if req.custom_speed is not None else None
    req_title = resolved_title
    req_chunk = req.tts_chunk_chars or 500
    req_verify = bool(req.verify_final_script)
    req_depth = (req.research_depth or "").lower().strip()

    duplicate_job = None
    for c_job in existing_candidates:
        c_mode = c_job.request_mode
        c_depth = (c_job.research_depth or "").lower().strip()
        c_voice = (c_job.custom_voice or c_job.kokoro_voice or "").strip()
        c_speed = (
            round(float(c_job.custom_speed or c_job.kokoro_speed), 2)
            if (c_job.custom_speed or c_job.kokoro_speed) is not None
            else None
        )
        c_title = (c_job.custom_title or "").strip()
        c_chunk = c_job.tts_chunk_chars if c_job.tts_chunk_chars is not None else 500
        c_verify = bool(c_job.verify_final_script)

        if (
            c_mode == mode_val
            and c_depth == req_depth
            and c_voice == req_voice
            and c_speed == req_speed
            and c_title == req_title
            and c_chunk == req_chunk
            and c_verify == req_verify
        ):
            # If the candidate is complete, only treat as duplicate if local MP3 is present
            if c_job.status == JobState.COMPLETE.value:
                if c_job.local_audio_path and os.path.exists(c_job.local_audio_path):
                    duplicate_job = c_job
                    break
                else:
                    # Audio cleaned up - do not treat as duplicate; allow creating a new job
                    continue
            else:
                duplicate_job = c_job
                break

    if duplicate_job:
        ep_title = _resolve_response_title(duplicate_job)
        return HeraldResponse(
            job_id=duplicate_job.id,
            status=duplicate_job.status,
            request_mode=duplicate_job.request_mode,
            source_type=duplicate_job.source_type,
            is_duplicate=True,
            message="Identical source content and settings already processed.",
            episode_title=ep_title,
        )

    # 4. Create PodcastJob
    job_id = str(uuid.uuid4())
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
        custom_title=req_title,
        tts_chunk_chars=req.tts_chunk_chars or 500,
        verify_final_script=req.verify_final_script,
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
                    message="Telegram message already accepted.",
                    episode_title=ep_title,
                )
        raise e

    transition_job_state(db, job, JobState.VALIDATING.value, component="herald-core")
    transition_job_state(db, job, JobState.SOURCE_READY.value, component="herald-core")
    transition_job_state(db, job, JobState.SCRIPTING.value, component="herald-core")

    # 5. Generate Script
    try:
        if mode_val == RequestMode.LITERAL.value:
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
            # Multi-stage grounded research workflow
            if not job.research_grounding_json:
                grounded_data = generate_grounded_research(
                    source_text=job.source_text,
                    research_depth=job.research_depth or "medium",
                    job_id=job.id,
                )
                job.research_grounding_json = grounded_data
                job.research_search_count = grounded_data.get("search_count", 0)
                job.research_source_count = grounded_data.get("source_count", 0)
                db.commit()

            if not job.research_json:
                dossier = normalize_research_dossier(
                    source_text=job.source_text,
                    grounded_research_data=job.research_grounding_json,
                    job_id=job.id,
                )
                job.research_json = dossier.model_dump()
                job.research_model = settings.GEMINI_RESEARCH_MODEL
                db.commit()

            if not job.script_json:
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
                audit = audit_research_script(
                    source_text=job.source_text,
                    research_dossier=job.research_json,
                    script_dict=job.script_json,
                    job_id=job.id,
                )
                job.research_audit_json = audit.model_dump()
                db.commit()

            audit_data = job.research_audit_json or {}
            if audit_data.get("has_material_issues") and job.research_repair_count == 0:
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
        else:
            # Brief or Standard AI mode
            t_script0 = datetime.now(UTC)
            provider = get_ai_provider()
            if not provider:
                raise GeminiError("AI provider is not configured.")
            script_resp = provider.generate_script(
                source_text=job.source_text,
                request_mode=mode_val,
                source_title=job.custom_title,
                job_id=job.id,
            )
            job.script_json = script_resp.model_dump()
            job.gemini_model = getattr(provider, "model_name", settings.GEMINI_MODEL)
            db.commit()
            record_stage_metric(
                job_id=job.id,
                stage="AI_SCRIPT",
                started_at=t_script0,
                finished_at=datetime.now(UTC),
                status="success",
                input_chars=len(job.source_text or ""),
            )

        transition_job_state(db, job, JobState.SCRIPT_READY.value, component="herald-core")

        script_obj = job.script_json or {}
        ep_title = _resolve_response_title(
            job, custom_title=job.custom_title, script_obj=script_obj
        )
        dur_info = calculate_script_duration(script_obj, job.custom_speed or settings.KOKORO_SPEED)

        if req.hold_for_approval:
            job.approval_required = True
            job.approval_requested_at = None
            job.telegram_approval_message_id = None
            transition_job_state(db, job, JobState.AWAITING_APPROVAL.value, component="herald-core")
            db.commit()
            return HeraldResponse(
                job_id=job.id,
                status=job.status,
                request_mode=job.request_mode,
                source_type=job.source_type,
                is_duplicate=False,
                message="Script ready and awaiting approval.",
                episode_title=ep_title,
                estimated_minutes=dur_info.get("estimated_minutes"),
            )

        transition_job_state(db, job, JobState.QUEUED_TTS.value, component="herald-core")
        db.commit()

        return HeraldResponse(
            job_id=job.id,
            status=job.status,
            request_mode=job.request_mode,
            source_type=job.source_type,
            is_duplicate=False,
            message="Accepted and queued for TTS synthesis.",
            episode_title=ep_title,
            estimated_minutes=dur_info.get("estimated_minutes"),
        )
    except Exception as e:
        logger.error(f"Script generation failure for job '{job.id}': {e}")
        transition_job_state(
            db,
            job,
            JobState.FAILED_FINAL.value,
            component="herald-core",
            message=str(e),
            error_category="SCRIPT_GENERATION_FAILED",
        )
        return HeraldResponse(
            job_id=job.id,
            status=job.status,
            request_mode=job.request_mode,
            source_type=job.source_type,
            is_duplicate=False,
            message=f"Script generation failed: {e}",
            error_category="SCRIPT_GENERATION_FAILED",
        )
