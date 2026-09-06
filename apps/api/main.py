import logging
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from herald.audio.artifact_generator import (
    ensure_details_artifact,
    get_artifact_filenames,
    get_required_artifact_types,
)
from herald.audio.ffmpeg_builder import check_free_disk_mb
from herald.config import settings
from herald.db.connection import get_db
from herald.db.models import JobProcessingMetric, JobState, PodcastJob, SourceType
from herald.db.state_machine import transition_job_state
from herald.extraction.email_parser import (
    SourceClassification,
    compute_source_hash,
    process_email_message,
)
from herald.extraction.source_cleaner import clean_source_text, deduplicate_source_blocks
from herald.extraction.url_extractor import (
    ArticleExtractionError,
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
from herald.services.drive_service import build_user_facing_drive_filename
from herald.services.email_formatter import (
    format_acknowledgment_email,
    format_completion_email,
    format_failure_email,
)
from herald.services.eta_calculator import calculate_job_eta, calculate_script_duration
from herald.services.performance_metrics import (
    record_stage_metric,
)
from herald.tts.kokoro_client import KokoroClient

logger = logging.getLogger("herald.api")

app = FastAPI(
    title="Herald Email-to-Podcast Automation API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


def verify_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")):
    """
    Constant-time API key verification using fail-closed security.
    """
    if settings.HERALD_ENV.lower() == "production":
        if not x_api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="X-API-Key header is missing.",
            )
        import secrets
        if not secrets.compare_digest(x_api_key, settings.HERALD_API_KEY):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid API Key.",
            )
    return True


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime object is timezone-aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def normalize_gmail_timestamp(raw_val: Any) -> datetime | None:
    """
    Parse Gmail timestamp (epoch ms string/int, RFC 2822, or ISO-8601 string)
    and return UTC datetime. Fall back to None without throwing exceptions.
    """
    if raw_val is None:
        return None

    try:
        if isinstance(raw_val, (int, float)):
            val_num = float(raw_val)
            if val_num > 1e11:  # epoch milliseconds
                val_num /= 1000.0
            return datetime.fromtimestamp(val_num, tz=UTC)

        val_str = str(raw_val).strip()
        if not val_str:
            return None

        if val_str.isdigit():
            val_num = float(val_str)
            if val_num > 1e11:  # epoch milliseconds
                val_num /= 1000.0
            return datetime.fromtimestamp(val_num, tz=UTC)

        from email.utils import parsedate_to_datetime
        try:
            dt = parsedate_to_datetime(val_str)
            if dt:
                return dt.astimezone(UTC)
        except Exception:
            pass

        dt = datetime.fromisoformat(val_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


class IntakeRequest(BaseModel):
    gmail_message_id: str = Field(..., description="Unique Gmail message ID")
    gmail_thread_id: str | None = Field(None, description="Gmail thread ID")
    sender_email: str = Field(..., description="Authorized sender email address")
    subject: str = Field(..., description="Email subject line containing Podcast: <Mode>")
    body_text: str | None = Field(None, description="Plain text email body")
    body_html: str | None = Field(None, description="HTML email body")
    gmail_received_at: Any | None = Field(None, description="Original Gmail message receipt timestamp")


class IntakeResponse(BaseModel):
    job_id: str
    status: str
    request_mode: str
    source_type: str
    is_duplicate: bool
    message: str
    acknowledgment_email_text: str | None = None
    acknowledgment_email_html: str | None = None
    failure_email_text: str | None = None
    failure_email_html: str | None = None
    error_category: str | None = None


class ExtractUrlRequest(BaseModel):
    url: str = Field(..., description="Public article URL to extract")


class ExtractUrlResponse(BaseModel):
    title: str
    extracted_text: str
    canonical_url: str


class GenerateScriptRequest(BaseModel):
    job_id: str = Field(..., description="Podcast job ID")


class DriveCompleteRequest(BaseModel):
    artifact_type: str = Field(default="audio", description="Artifact type: audio or details")
    drive_file_id: str | None = Field(None, description="Uploaded Google Drive file ID for audio MP3")
    drive_web_link: str | None = Field(None, description="Web link to audio Google Drive file")
    details_drive_file_id: str | None = Field(None, description="Uploaded Google Drive file ID for details Markdown")
    details_drive_web_link: str | None = Field(None, description="Web link to details Markdown Google Drive file")
    started_at: Any | None = Field(None, description="Drive upload start timestamp")
    finished_at: Any | None = Field(None, description="Drive upload finish timestamp")
    duration_ms: int | None = Field(None, description="Drive upload duration in milliseconds")
    drive_job_key: str | None = Field(None, description="Herald job key stored in Drive appProperties")

    # Legacy fields preserved for schema compatibility
    source_drive_file_id: str | None = None
    source_drive_web_link: str | None = None
    script_drive_file_id: str | None = None
    script_drive_web_link: str | None = None
    diagnostics_drive_file_id: str | None = None
    diagnostics_drive_web_link: str | None = None
    research_drive_file_id: str | None = None
    research_drive_web_link: str | None = None
    research_notes_drive_file_id: str | None = None
    research_notes_drive_web_link: str | None = None


class DeliveryCompleteRequest(BaseModel):
    gmail_result_message_id: str | None = Field(None, description="Sent reply Gmail message ID")
    started_at: Any | None = Field(None, description="Email delivery start timestamp")
    finished_at: Any | None = Field(None, description="Email delivery finish timestamp")
    duration_ms: int | None = Field(None, description="Email delivery duration in milliseconds")


class DeliveryFailedRequest(BaseModel):
    error_code: str = Field(default="GMAIL_DELIVERY_FAILURE")
    error_detail: str = Field(default="Failed to deliver completion email")


class JobStatusResponse(BaseModel):
    id: str
    transport: str = "email"
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None
    sender_email: str | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    telegram_user_id: int | None = None
    request_mode: str
    research_depth: str | None = None
    source_type: str
    source_url: str | None
    status: str
    attempt_count: int
    synthesis_attempt_count: int
    delivery_attempt_count: int
    completed_chunk_index: int
    local_audio_path: str | None
    audio_bytes: int | None
    audio_sha256: str | None
    audio_duration_seconds: int | None
    drive_file_id: str | None
    drive_web_link: str | None
    details_drive_file_id: str | None = None
    details_drive_web_link: str | None = None
    source_drive_file_id: str | None = None
    source_drive_web_link: str | None = None
    script_drive_file_id: str | None = None
    script_drive_web_link: str | None = None
    diagnostics_drive_file_id: str | None = None
    diagnostics_drive_web_link: str | None = None
    research_drive_file_id: str | None = None
    research_drive_web_link: str | None = None
    research_notes_drive_file_id: str | None = None
    research_notes_drive_web_link: str | None = None
    drive_job_key: str | None
    gmail_result_message_id: str | None
    kokoro_voice: str | None
    kokoro_speed: float | None
    gemini_model: str | None
    research_model: str | None = None
    research_search_count: int | None = None
    research_source_count: int | None = None
    research_repair_count: int | None = 0
    error_code: str | None
    error_detail: str | None
    created_at: str
    updated_at: str
    gmail_received_at: str | None = None
    audio_ready_at: str | None = None
    drive_uploaded_at: str | None = None
    delivered_at: str | None = None
    completed_at: str | None = None



@app.get("/health", tags=["Health"])
@app.get("/live", tags=["Health"])
def health_check():
    """Process liveness check endpoint. Returns HTTP 200 when API process is running."""
    c_cfg = settings.get_concurrency_config()
    return {
        "status": "live",
        "timestamp": datetime.now(UTC).isoformat(),
        "environment": settings.HERALD_ENV,
        "concurrency_profile": c_cfg.profile,
        "detected_cpus": c_cfg.detected_cpus,
    }


@app.get("/readiness", tags=["Health"])
@app.get("/ready", tags=["Health"])
def readiness_check(db: Session = Depends(get_db)):
    """
    Readiness evaluation validating PostgreSQL, expected schema revision, API key, allowlist, work dir, free disk, and Kokoro.
    Returns HTTP 503 if any required check fails.
    """
    reasons = []

    # 1. PostgreSQL DB check
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        reasons.append(f"Database connection failed: {e}")

    # 2. Alembic schema check
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        alembic_cfg = Config("alembic.ini")
        script_dir = ScriptDirectory.from_config(alembic_cfg)
        head_revision = script_dir.get_current_head()

        result = db.execute(text("SELECT version_num FROM alembic_version")).first()
        current_revision = result[0] if result else None
        if not current_revision:
            reasons.append("Database schema missing alembic_version revision")
        elif current_revision != head_revision:
            reasons.append(f"Database revision ({current_revision}) does not match expected head ({head_revision})")
    except Exception as e:
        reasons.append(f"Alembic version check failed: {e}")

    # 3. Production security check
    if settings.HERALD_ENV.lower() == "production":
        is_email_active = bool(settings.ENABLE_EMAIL_TRANSPORT or settings.EMAIL_ALLOWED_SENDERS.strip() or settings.GOOGLE_DRIVE_FOLDER_ID.strip())
        if is_email_active:
            if not settings.HERALD_API_KEY or settings.HERALD_API_KEY == "default-insecure-api-key":
                reasons.append("Production HERALD_API_KEY is not configured securely for active Email/API transport")
            if not settings.EMAIL_ALLOWED_SENDERS.strip():
                reasons.append("Production EMAIL_ALLOWED_SENDERS is empty for active Email transport (fail-closed rule)")
            if not settings.GOOGLE_DRIVE_FOLDER_ID.strip():
                reasons.append("Production GOOGLE_DRIVE_FOLDER_ID is empty for active Drive transport")
        elif not settings.TELEGRAM_BOT_TOKEN:
            reasons.append("No active transport configured (neither Telegram nor Email/Drive configured)")

    # 4. Work directory writability check
    work_dir = Path(settings.HERALD_WORK_DIR)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        test_file = work_dir / ".readiness_test"
        test_file.touch()
        test_file.unlink(missing_ok=True)
    except Exception as e:
        reasons.append(f"Work directory '{work_dir}' is not writable: {e}")

    # 5. Free disk check with fail-closed exception handling
    try:
        free_mb = check_free_disk_mb(work_dir)
        if free_mb < settings.HERALD_MIN_DISK_MB:
            reasons.append(f"Low free disk space ({free_mb:.1f} MB available, required {settings.HERALD_MIN_DISK_MB} MB)")
    except Exception as e:
        free_mb = 0.0
        reasons.append(f"Disk space inspection failed: {e}")

    # 6. Kokoro check - inspect kokoro_res["healthy"]
    try:
        kokoro_client = KokoroClient()
        kokoro_res = kokoro_client.health_check()
        kokoro_healthy = bool(isinstance(kokoro_res, dict) and kokoro_res.get("healthy"))
        if not kokoro_healthy and (settings.HERALD_ENV.lower() == "production" or os.environ.get("HERALD_REQUIRE_KOKORO") == "1"):
            reasons.append("Kokoro TTS service reachable check failed")
    except Exception as e:
        kokoro_healthy = False
        if settings.HERALD_ENV.lower() == "production" or os.environ.get("HERALD_REQUIRE_KOKORO") == "1":
            reasons.append(f"Kokoro health check exception: {e}")

    if reasons:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"ready": False, "reasons": reasons},
        )

    c_cfg = settings.get_concurrency_config()
    return {
        "ready": True,
        "environment": settings.HERALD_ENV,
        "free_disk_mb": free_mb,
        "kokoro_tts": kokoro_healthy,
        "concurrency": {
            "profile": c_cfg.profile,
            "detected_cpus": c_cfg.detected_cpus,
            "worker_concurrency": c_cfg.worker_concurrency,
            "script_concurrency": c_cfg.script_concurrency,
            "tts_global_slots": c_cfg.tts_global_slots,
            "tts_per_job": c_cfg.tts_per_job,
            "ffmpeg_concurrency": c_cfg.ffmpeg_concurrency,
        },
    }



@app.post(
    "/api/v1/intake",
    response_model=IntakeResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["Intake"],
)
def process_intake(req: IntakeRequest, db: Session = Depends(get_db)):
    """Validate sender allowlist, check message deduplication, clean content, parse directives, and create job."""
    t_intake_start = datetime.now(UTC)
    sender = req.sender_email.strip().lower()
    allowed_senders = settings.get_allowed_senders_list()

    if settings.HERALD_ENV.lower() == "production" and not allowed_senders:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Security Violation: Sender allowlist is empty in production environment.",
        )

    if allowed_senders and sender not in allowed_senders:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Security Violation: Sender '{sender}' is not on the authorized allowlist.",
        )

    existing_message_job = (
        db.query(PodcastJob)
        .filter(PodcastJob.gmail_message_id == req.gmail_message_id)
        .first()
    )
    if existing_message_job:
        return IntakeResponse(
            job_id=existing_message_job.id,
            status=existing_message_job.status,
            request_mode=existing_message_job.request_mode,
            source_type=existing_message_job.source_type,
            is_duplicate=True,
            message="Message ID has already been processed.",
        )

    try:
        parsed = process_email_message(
            subject=req.subject, body_text=req.body_text, body_html=req.body_html
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Email intake processing failed: {e}",
        )

    source_type = SourceType.EMAIL_BODY.value
    source_url = None

    if parsed.classification == SourceClassification.URL and parsed.detected_url:
        source_type = SourceType.URL.value
        source_url = parsed.detected_url

    # 2. Check for duplicate content AND matching generation settings
    if source_url:
        source_filter = or_(
            PodcastJob.source_hash == parsed.source_hash,
            and_(PodcastJob.source_url.isnot(None), PodcastJob.source_url == source_url),
        )
    else:
        source_filter = PodcastJob.source_hash == parsed.source_hash

    candidate_jobs = (
        db.query(PodcastJob)
        .filter(source_filter)
        .filter(PodcastJob.status != JobState.FAILED_FINAL.value)
        .all()
    )

    req_mode = parsed.mode.value
    req_depth = (parsed.research_depth or "").lower().strip()
    req_voice = (parsed.custom_voice or "").strip()
    req_speed = round(float(parsed.custom_speed), 2) if parsed.custom_speed is not None else None
    req_title = (parsed.custom_title or "").strip()
    req_chunk = parsed.tts_chunk_chars or 500
    req_verify = bool(parsed.verify_final_script)

    duplicate_job = None
    for c_job in candidate_jobs:
        c_mode = c_job.request_mode
        c_depth = (c_job.research_depth or "").lower().strip()
        c_voice = (c_job.custom_voice or c_job.kokoro_voice or "").strip()
        c_speed = round(float(c_job.custom_speed or c_job.kokoro_speed), 2) if (c_job.custom_speed or c_job.kokoro_speed) is not None else None
        c_title = (c_job.custom_title or "").strip()
        c_chunk = c_job.tts_chunk_chars if c_job.tts_chunk_chars is not None else 500
        c_verify = bool(c_job.verify_final_script)

        if (
            c_mode == req_mode
            and c_depth == req_depth
            and c_voice == req_voice
            and c_speed == req_speed
            and c_title == req_title
            and c_chunk == req_chunk
            and c_verify == req_verify
        ):
            duplicate_job = c_job
            break

    if duplicate_job:
        return IntakeResponse(
            job_id=duplicate_job.id,
            status=duplicate_job.status,
            request_mode=duplicate_job.request_mode,
            source_type=duplicate_job.source_type,
            is_duplicate=True,
            message="Identical source content and generation settings already processed.",
        )

    job_id = str(uuid.uuid4())
    drive_key = f"herald_job_{job_id}"
    parsed_gmail_received = normalize_gmail_timestamp(req.gmail_received_at)

    job = PodcastJob(
        id=job_id,
        gmail_message_id=req.gmail_message_id,
        gmail_thread_id=req.gmail_thread_id,
        sender_email=sender,
        gmail_received_at=parsed_gmail_received,
        request_mode=parsed.mode.value,
        research_depth=parsed.research_depth,
        source_type=source_type,
        source_url=source_url,
        source_hash=parsed.source_hash,
        source_text=parsed.clean_text,
        custom_voice=parsed.custom_voice,
        custom_speed=parsed.custom_speed,
        custom_title=parsed.custom_title,
        tts_chunk_chars=parsed.tts_chunk_chars,
        verify_final_script=parsed.verify_final_script,
        drive_job_key=drive_key,
        status=JobState.RECEIVED.value,
    )

    try:
        db.add(job)
        db.commit()
        db.refresh(job)
    except IntegrityError as ie:
        db.rollback()
        if req.gmail_message_id and req.gmail_message_id.strip():
            racing_job = (
                db.query(PodcastJob)
                .filter(PodcastJob.gmail_message_id == req.gmail_message_id.strip())
                .first()
            )
            if racing_job:
                return IntakeResponse(
                    job_id=racing_job.id,
                    status=racing_job.status,
                    request_mode=racing_job.request_mode,
                    source_type=racing_job.source_type,
                    is_duplicate=True,
                    message="Message ID has already been processed.",
                )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Database constraint collision on intake: {ie}",
        )

    # Record Intake & Email Detection Wait metrics safely
    t_intake_finish = datetime.now(UTC)
    record_stage_metric(
        job_id=job.id,
        stage="INTAKE_TOTAL",
        started_at=t_intake_start,
        finished_at=t_intake_finish,
        status="success",
        input_chars=len(req.body_text or req.body_html or ""),
    )

    if parsed_gmail_received:
        wait_ms = max(0, int((t_intake_start - ensure_utc(parsed_gmail_received)).total_seconds() * 1000))
        record_stage_metric(
            job_id=job.id,
            stage="EMAIL_DETECTION_WAIT",
            started_at=parsed_gmail_received,
            finished_at=t_intake_start,
            duration_ms=wait_ms,
            status="success",
            metadata_json={"baseline": "gmail_received_at"},
        )

    transition_job_state(db, job, JobState.VALIDATING.value, component="herald-api")

    # Perform Source Normalization & Deduplication for Email Body text if present
    if job.source_text and source_type == SourceType.EMAIL_BODY.value:
        t_norm0 = datetime.now(UTC)
        deduped_text, norm_stats = deduplicate_source_blocks(job.source_text)
        job.source_text = deduped_text
        job.source_hash = compute_source_hash(deduped_text, None)
        db.commit()
        record_stage_metric(
            job_id=job.id,
            stage="SOURCE_NORMALIZATION",
            started_at=t_norm0,
            finished_at=datetime.now(UTC),
            status="success",
            input_chars=norm_stats["original_char_count"],
            output_bytes=norm_stats["normalized_char_count"],
            metadata_json=norm_stats,
        )

    if source_type == SourceType.URL.value and source_url:
        transition_job_state(db, job, JobState.EXTRACTING.value, component="herald-api")
        t_url0 = datetime.now(UTC)
        try:
            title, extracted_text, canonical_url = extract_article_from_url(source_url)
            cleaned_extracted = clean_source_text(extracted_text)
            deduped_extracted, norm_stats = deduplicate_source_blocks(cleaned_extracted)
            job.source_text = f"Title: {title}\n\n{deduped_extracted}"
            job.source_url = canonical_url
            job.source_hash = compute_source_hash(deduped_extracted, canonical_url)
            db.commit()
            record_stage_metric(
                job_id=job.id,
                stage="URL_EXTRACTION",
                started_at=t_url0,
                finished_at=datetime.now(UTC),
                status="success",
                input_chars=len(source_url),
                output_bytes=len(job.source_text.encode("utf-8")),
            )
            record_stage_metric(
                job_id=job.id,
                stage="SOURCE_NORMALIZATION",
                started_at=t_url0,
                finished_at=datetime.now(UTC),
                status="success",
                input_chars=norm_stats["original_char_count"],
                output_bytes=norm_stats["normalized_char_count"],
                metadata_json=norm_stats,
            )
        except SourceAccessBlockedError as sbe:
            record_stage_metric(
                job_id=job.id,
                stage="URL_EXTRACTION",
                started_at=t_url0,
                finished_at=datetime.now(UTC),
                status="failed",
                metadata_json={"error": str(sbe), "category": "SOURCE_ACCESS_BLOCKED"},
            )
            transition_job_state(
                db, job, JobState.FAILED_FINAL.value, component="herald-api", message=str(sbe), error_category="SOURCE_ACCESS_BLOCKED"
            )
            fail_email = format_failure_email(job.id, source_url, "SOURCE_ACCESS_BLOCKED", str(sbe))
            return IntakeResponse(
                job_id=job.id,
                status=job.status,
                request_mode=job.request_mode,
                source_type=job.source_type,
                is_duplicate=False,
                message="Publisher blocked automated retrieval.",
                failure_email_text=fail_email["text"],
                failure_email_html=fail_email["html"],
                error_category="SOURCE_ACCESS_BLOCKED",
            )
        except DNSResolutionError as de:
            record_stage_metric(
                job_id=job.id,
                stage="URL_EXTRACTION",
                started_at=t_url0,
                finished_at=datetime.now(UTC),
                status="failed",
                metadata_json={"error": str(de), "category": "DNS_RESOLUTION_FAILURE"},
            )
            transition_job_state(
                db, job, JobState.FAILED_FINAL.value, component="herald-api", message=str(de), error_category="DNS_RESOLUTION_FAILURE"
            )
            fail_email = format_failure_email(job.id, source_url, "DNS_RESOLUTION_FAILURE", str(de))
            return IntakeResponse(
                job_id=job.id,
                status=job.status,
                request_mode=job.request_mode,
                source_type=job.source_type,
                is_duplicate=False,
                message=f"URL retrieval failed: {de}",
                failure_email_text=fail_email["text"],
                failure_email_html=fail_email["html"],
                error_category="DNS_RESOLUTION_FAILURE",
            )
        except SSRFVulnerabilityError as se:
            record_stage_metric(
                job_id=job.id,
                stage="URL_EXTRACTION",
                started_at=t_url0,
                finished_at=datetime.now(UTC),
                status="failed",
                metadata_json={"error": str(se), "category": "SSRF_PROTECTION"},
            )
            transition_job_state(
                db, job, JobState.FAILED_FINAL.value, component="herald-api", message=str(se), error_category="SSRF_PROTECTION"
            )
            fail_email = format_failure_email(job.id, source_url, "SSRF_PROTECTION", str(se))
            return IntakeResponse(
                job_id=job.id,
                status=job.status,
                request_mode=job.request_mode,
                source_type=job.source_type,
                is_duplicate=False,
                message=f"Security violation: {se}",
                failure_email_text=fail_email["text"],
                failure_email_html=fail_email["html"],
                error_category="SSRF_PROTECTION",
            )
        except ArticleExtractionError as e:
            record_stage_metric(
                job_id=job.id,
                stage="URL_EXTRACTION",
                started_at=t_url0,
                finished_at=datetime.now(UTC),
                status="failed",
                metadata_json={"error": str(e)},
            )
            transition_job_state(
                db, job, JobState.FAILED_FINAL.value, component="herald-api", message=str(e), error_category="ARTICLE_EXTRACTION_FAILURE"
            )
            fail_email = format_failure_email(job.id, source_url, "ARTICLE_EXTRACTION_FAILURE", str(e))
            return IntakeResponse(
                job_id=job.id,
                status=job.status,
                request_mode=job.request_mode,
                source_type=job.source_type,
                is_duplicate=False,
                message=f"URL extraction failed: {e}",
                failure_email_text=fail_email["text"],
                failure_email_html=fail_email["html"],
                error_category="ARTICLE_EXTRACTION_FAILURE",
            )

    transition_job_state(db, job, JobState.SOURCE_READY.value, component="herald-api")

    ack = format_acknowledgment_email(
        job_id=job.id,
        request_mode=job.request_mode,
        source_type=job.source_type,
        verify_enabled=bool(job.verify_final_script),
        created_at_iso=job.created_at.isoformat() if job.created_at else None,
        research_depth=job.research_depth if job.request_mode == "research" else None,
    )

    return IntakeResponse(
        job_id=job.id,
        status=job.status,
        request_mode=job.request_mode,
        source_type=job.source_type,
        is_duplicate=False,
        message="Intake successful and content normalized.",
        acknowledgment_email_text=ack["text"],
        acknowledgment_email_html=ack["html"],
    )


@app.post(
    "/api/v1/extract",
    response_model=ExtractUrlResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["Extraction"],
)
def extract_url(req: ExtractUrlRequest):
    """Safely extract public article text with SSRF protection."""
    try:
        title, extracted_text, canonical_url = extract_article_from_url(req.url)
        return ExtractUrlResponse(
            title=title, extracted_text=extracted_text, canonical_url=canonical_url
        )
    except SSRFVulnerabilityError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"SSRF Protection: {e}"
        )
    except ArticleExtractionError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        )


@app.post(
    "/api/v1/script/generate",
    dependencies=[Depends(verify_api_key)],
    tags=["Scripting"],
)
def generate_script_endpoint(req: GenerateScriptRequest, db: Session = Depends(get_db)):
    """Generate Gemini podcast script adhering to requested mode and transition job state to QUEUED_TTS."""
    job = db.query(PodcastJob).filter(PodcastJob.id == req.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status in (
        JobState.SCRIPTING.value,
        JobState.SCRIPT_READY.value,
        JobState.QUEUED_TTS.value,
        JobState.SYNTHESIZING.value,
        JobState.ENCODING.value,
        JobState.AUDIO_READY.value,
        JobState.UPLOADING.value,
        JobState.DELIVERING.value,
        JobState.COMPLETE.value,
    ) or (job.script_json and job.status != JobState.SOURCE_READY.value):
        return {"job_id": job.id, "status": job.status, "message": "Script already exists for job."}

    transition_job_state(db, job, JobState.SCRIPTING.value, component="herald-api")

    try:
        req_mode = (job.request_mode or "standard").lower()

        if req_mode == "literal":
            if not job.script_json:
                t0 = datetime.now(UTC)
                script = generate_literal_script(
                    source_text=job.source_text,
                    source_title=job.custom_title,
                    max_segment_chars=job.tts_chunk_chars or 1000,
                )
                t1 = datetime.now(UTC)
                job.script_json = script.model_dump()
                db.commit()
                record_stage_metric(
                    job_id=job.id,
                    stage="LITERAL_SCRIPT",
                    started_at=t0,
                    finished_at=t1,
                    status="success",
                    input_chars=len(job.source_text or ""),
                )
        elif req_mode == "research":
            from herald.ai.factory import get_research_provider
            research_prov = get_research_provider()
            if not research_prov or not research_prov.is_configured() or not research_prov.capabilities.research_grounding:
                r_name = getattr(settings, "RESEARCH_PROVIDER", "gemini")
                raise HTTPException(
                    status_code=400,
                    detail=f"Research mode requires a provider capable of Google Search Grounding (configured RESEARCH_PROVIDER='{r_name}').",
                )
            # Stage 1a: Grounded Research Call using GEMINI_RESEARCH_MODEL
            if not job.research_grounding_json:
                logger.info(f"Executing Stage 1a Grounded Research for job '{job.id}' (Depth: {job.research_depth})")

                t0 = datetime.now(UTC)
                grounded_data = generate_grounded_research(
                    source_text=job.source_text,
                    research_depth=job.research_depth or "medium",
                    job_id=job.id,
                )
                t1 = datetime.now(UTC)
                job.research_grounding_json = grounded_data
                job.research_search_count = grounded_data.get("search_count", 0)
                job.research_source_count = grounded_data.get("source_count", 0)
                db.commit()
                record_stage_metric(
                    job_id=job.id,
                    stage="RESEARCH_GROUNDING",
                    started_at=t0,
                    finished_at=t1,
                    status="success",
                    input_chars=len(job.source_text or ""),
                    metadata_json={"search_count": job.research_search_count, "source_count": job.research_source_count},
                )

            # Stage 1b: Normalize Research Dossier using GEMINI_MODEL
            if not job.research_json:
                logger.info(f"Executing Stage 1b Dossier Normalization for job '{job.id}'")
                t0 = datetime.now(UTC)
                dossier = normalize_research_dossier(
                    source_text=job.source_text,
                    grounded_research_data=job.research_grounding_json,
                    job_id=job.id,
                )
                t1 = datetime.now(UTC)
                job.research_json = dossier.model_dump()
                job.research_model = getattr(research_prov, "research_model", None) or settings.GEMINI_RESEARCH_MODEL
                db.commit()
                record_stage_metric(
                    job_id=job.id,
                    stage="RESEARCH_NORMALIZATION",
                    started_at=t0,
                    finished_at=t1,
                    status="success",
                )

            # Stage 2: Script Generation from SOURCE + VERIFIED_RESEARCH
            if not job.script_json:
                logger.info(f"Executing Stage 2 Research Scripting for job '{job.id}'")
                t0 = datetime.now(UTC)
                script = generate_podcast_script(
                    source_text=job.source_text,
                    request_mode="research",
                    research_dossier=job.research_json,
                    source_title=job.custom_title,
                    job_id=job.id,
                )
                t1 = datetime.now(UTC)
                job.script_json = script.model_dump()
                db.commit()
                record_stage_metric(
                    job_id=job.id,
                    stage="RESEARCH_SCRIPT",
                    started_at=t0,
                    finished_at=t1,
                    status="success",
                )

            # Stage 3: Post-Generation Research Audit
            if not job.research_audit_json:
                logger.info(f"Executing Stage 3 Research Audit for job '{job.id}'")
                t0 = datetime.now(UTC)
                audit = audit_research_script(
                    source_text=job.source_text,
                    research_dossier=job.research_json,
                    script_dict=job.script_json,
                    job_id=job.id,
                )
                t1 = datetime.now(UTC)
                job.research_audit_json = audit.model_dump()
                db.commit()
                record_stage_metric(
                    job_id=job.id,
                    stage="RESEARCH_AUDIT",
                    started_at=t0,
                    finished_at=t1,
                    status="success",
                    metadata_json={"has_material_issues": audit.has_material_issues},
                )

            # Stage 4: Single-Pass Script Repair if material issues found
            audit_data = job.research_audit_json or {}
            if audit_data.get("has_material_issues") and job.research_repair_count == 0:
                logger.info(f"Executing Stage 4 Targeted Script Repair for job '{job.id}'")
                t0 = datetime.now(UTC)
                repaired_script = repair_research_script(
                    source_text=job.source_text,
                    research_dossier=job.research_json,
                    script_dict=job.script_json,
                    audit_result=audit_data,
                    job_id=job.id,
                )
                t1 = datetime.now(UTC)
                job.script_json = repaired_script.model_dump()
                job.research_repair_count = 1
                db.commit()
                record_stage_metric(
                    job_id=job.id,
                    stage="RESEARCH_REPAIR",
                    started_at=t0,
                    finished_at=t1,
                    status="success",
                    is_attempt_metric=True,
                )

            # Optional final script verification for Research mode when verify=true
            if job.verify_final_script and not job.verify_audit_json:
                logger.info(f"Executing Optional Final VERIFY Audit for Research job '{job.id}'")
                t0 = datetime.now(UTC)
                v_audit = audit_script_fidelity(
                    source_text=job.source_text,
                    script_dict=job.script_json,
                    job_id=job.id,
                )
                t1 = datetime.now(UTC)
                job.verify_audit_json = v_audit.model_dump()
                db.commit()
                record_stage_metric(
                    job_id=job.id,
                    stage="VERIFY_AUDIT",
                    started_at=t0,
                    finished_at=t1,
                    status="success",
                    metadata_json={"has_material_issues": v_audit.has_material_issues},
                )

                if v_audit.has_material_issues and job.verify_repair_count == 0:
                    logger.info(f"Executing Optional Final VERIFY Repair for Research job '{job.id}'")
                    t0 = datetime.now(UTC)
                    repaired_script = repair_research_script(
                        source_text=job.source_text,
                        research_dossier=job.research_json or {},
                        script_dict=job.script_json,
                        audit_result=v_audit.model_dump(),
                        job_id=job.id,
                    )
                    t1 = datetime.now(UTC)
                    job.script_json = repaired_script.model_dump()
                    job.verify_repair_count = 1
                    db.commit()
                    record_stage_metric(
                        job_id=job.id,
                        stage="VERIFY_REPAIR",
                        started_at=t0,
                        finished_at=t1,
                        status="success",
                        is_attempt_metric=True,
                    )

        else:
            # Brief or Standard mode
            if not job.script_json:
                t0 = datetime.now(UTC)
                from herald.ai.factory import get_ai_provider
                provider = get_ai_provider()
                if not provider or not provider.is_configured():
                    raise HTTPException(
                        status_code=400,
                        detail=f"AI provider '{settings.AI_PROVIDER}' is not configured. Configure an AI API key or use literal mode.",
                    )
                script_resp = provider.generate_script(
                    source_text=job.source_text,
                    request_mode=req_mode,
                    source_title=job.custom_title,
                    job_id=job.id,
                )
                t1 = datetime.now(UTC)
                job.script_json = script_resp.model_dump()
                job.gemini_model = provider.configured_model
                db.commit()
                record_stage_metric(
                    job_id=job.id,
                    stage="AI_SCRIPT",
                    started_at=t0,
                    finished_at=t1,
                    status="success",
                    input_chars=len(job.source_text or ""),
                )

            # Optional script verification for Brief/Standard mode when verify=true
            if job.verify_final_script and not job.verify_audit_json:
                logger.info(f"Executing VERIFY Audit for {req_mode.upper()} job '{job.id}'")
                t0 = datetime.now(UTC)
                v_audit = audit_script_fidelity(
                    source_text=job.source_text,
                    script_dict=job.script_json,
                    job_id=job.id,
                )
                t1 = datetime.now(UTC)
                job.verify_audit_json = v_audit.model_dump()
                db.commit()
                record_stage_metric(
                    job_id=job.id,
                    stage="VERIFY_AUDIT",
                    started_at=t0,
                    finished_at=t1,
                    status="success",
                    metadata_json={"has_material_issues": v_audit.has_material_issues},
                )

                if v_audit.has_material_issues and job.verify_repair_count == 0:
                    logger.info(f"Executing VERIFY Repair for {req_mode.upper()} job '{job.id}'")
                    t0 = datetime.now(UTC)
                    repaired = repair_script_fidelity(
                        source_text=job.source_text,
                        script_dict=job.script_json,
                        audit_result=v_audit.model_dump(),
                        job_id=job.id,
                    )
                    t1 = datetime.now(UTC)
                    job.script_json = repaired.model_dump()
                    job.verify_repair_count = 1
                    db.commit()
                    record_stage_metric(
                        job_id=job.id,
                        stage="VERIFY_REPAIR",
                        started_at=t0,
                        finished_at=t1,
                        status="success",
                        is_attempt_metric=True,
                    )

        transition_job_state(db, job, JobState.SCRIPT_READY.value, component="herald-api")
        transition_job_state(db, job, JobState.QUEUED_TTS.value, component="herald-api")

        script_obj = job.script_json or {}
        episode_title = script_obj.get("episode_title", job.custom_title or "Herald Episode")
        segments = script_obj.get("segments", [])

        dur_info = calculate_script_duration(script_obj, job.custom_speed or settings.KOKORO_SPEED)
        eta_info = calculate_job_eta(db, job)

        return {
            "job_id": job.id,
            "gmail_message_id": job.gmail_message_id,
            "status": job.status,
            "episode_title": episode_title,
            "request_mode": job.request_mode,
            "research_depth": job.research_depth,
            "estimated_minutes": dur_info["estimated_minutes"],
            "estimated_completion_range": eta_info["estimated_completion_range"],
            "segments_count": len(segments),
        }
    except GeminiError as e:
        transition_job_state(
            db, job, JobState.FAILED_RETRYABLE.value, component="herald-api", message=str(e), error_category="GEMINI_SCRIPT_FAILURE"
        )
        raise HTTPException(status_code=500, detail=f"Gemini scripting failed: {e}")


@app.get(
    "/api/v1/jobs/{job_id}/eta",
    dependencies=[Depends(verify_api_key)],
    tags=["Jobs"],
)
def get_job_eta_endpoint(job_id: str, db: Session = Depends(get_db)):
    """Calculate best-effort job completion ETA range accounting for queue work ahead."""
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return calculate_job_eta(db, job)


@app.post(
    "/api/v1/delivery/claim",
    dependencies=[Depends(verify_api_key)],
    tags=["Delivery"],
)
def claim_delivery_job(db: Session = Depends(get_db)):
    """
    Atomically select 1 eligible delivery job using SELECT FOR UPDATE SKIP LOCKED on 1 row.
    Returns canonical filenames, local paths, Drive IDs, and explicit upload flags for ["audio", "details"].
    """
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(minutes=15)

    eligible_filter = or_(
        PodcastJob.status == JobState.AUDIO_READY.value,
        and_(
            PodcastJob.status.in_([JobState.UPLOADING.value, JobState.DELIVERING.value]),
            or_(PodcastJob.last_heartbeat_at.is_(None), PodcastJob.last_heartbeat_at <= stale_cutoff),
            or_(PodcastJob.claimed_at.is_(None), PodcastJob.claimed_at <= stale_cutoff),
        ),
        and_(
            PodcastJob.status == JobState.FAILED_RETRYABLE.value,
            PodcastJob.failed_stage.notin_(["INTAKE", "VALIDATING", "EXTRACTING", "SCRIPTING", "SYNTHESIZING", "ENCODING"]),
            or_(PodcastJob.next_retry_at.is_(None), PodcastJob.next_retry_at <= now),
        ),
    )

    eligible_job = (
        db.query(PodcastJob)
        .filter(eligible_filter)
        .order_by(PodcastJob.updated_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )

    if not eligible_job:
        return {"claimed": False, "action": "none", "job": None}

    job = eligible_job
    job.claimed_at = now
    job.claim_owner = "n8n-completion-dispatcher"
    job.delivery_attempt_count += 1
    job.updated_at = now

    work_dir = Path(settings.HERALD_WORK_DIR)
    output_dir = work_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Capture DELIVERY_DISPATCH_WAIT metrics data before commit
    audio_ready_at_val = job.audio_ready_at
    wait_ms = None
    if audio_ready_at_val:
        wait_ms = max(0, int((now - ensure_utc(audio_ready_at_val)).total_seconds() * 1000))

    # Generate unified companion details artifact
    ensure_details_artifact(job, output_dir, db=db)
    names = get_artifact_filenames(job)

    local_audio_path = job.local_audio_path or str(output_dir / names["audio_filename"])
    local_details_path = str(output_dir / names["details_filename"])

    ep_title = (job.script_json or {}).get("episode_title") or job.custom_title or "Herald Episode"
    audio_drive_filename = build_user_facing_drive_filename(ep_title, job.created_at, job.request_mode, "mp3")
    details_drive_filename = build_user_facing_drive_filename(ep_title, job.created_at, job.request_mode, "md")

    needs_audio_upload = not bool(job.drive_file_id and job.drive_web_link)
    needs_details_upload = not bool(job.details_drive_file_id and job.details_drive_web_link)

    needs_email = not bool(job.delivered_at or job.gmail_result_message_id)
    needs_upload = needs_audio_upload or needs_details_upload

    if needs_email or needs_upload:
        action = "deliver_artifacts_and_email"
        if job.status == JobState.DELIVERING.value:
            target_state = JobState.DELIVERING.value
        elif needs_upload:
            target_state = JobState.UPLOADING.value
        else:
            target_state = JobState.DELIVERING.value
    else:
        action = "complete_without_resend"
        target_state = JobState.COMPLETE.value

    old_state = job.status
    if old_state != target_state:
        transition_job_state(
            db,
            job,
            target_state,
            component="n8n-delivery-claim",
            message=f"Claimed delivery job atomically for action '{action}'",
            commit=False,
        )

    db.commit()
    db.refresh(job)

    # Record DELIVERY_DISPATCH_WAIT metric AFTER db.commit() to release row lock
    if audio_ready_at_val and wait_ms is not None:
        record_stage_metric(
            job_id=job.id,
            stage="DELIVERY_DISPATCH_WAIT",
            started_at=audio_ready_at_val,
            finished_at=now,
            duration_ms=wait_ms,
            status="success",
        )

    return {
        "claimed": True,
        "action": action,
        "job": {
            "id": job.id,
            "gmail_message_id": job.gmail_message_id,
            "gmail_thread_id": job.gmail_thread_id,
            "sender_email": job.sender_email,
            "request_mode": job.request_mode,
            "status": job.status,
            "delivery_attempt_count": job.delivery_attempt_count,
            "local_audio_path": local_audio_path,
            "audio_filename": names["audio_filename"],
            "audio_drive_filename": audio_drive_filename,
            "local_details_path": local_details_path,
            "details_filename": names["details_filename"],
            "details_drive_filename": details_drive_filename,
            "needs_audio_upload": needs_audio_upload,
            "needs_details_upload": needs_details_upload,
            "needs_email": needs_email,
            "drive_file_id": job.drive_file_id,
            "drive_web_link": job.drive_web_link,
            "details_drive_file_id": job.details_drive_file_id,
            "details_drive_web_link": job.details_drive_web_link,
            "drive_job_key": job.drive_job_key or f"herald_job_{job.id}",
            "script_json": job.script_json,
            "needs_source_upload": False,
            "needs_script_upload": False,
            "needs_diagnostics_upload": False,
            "needs_research_upload": False,
            "needs_research_notes_upload": False,
            "needs_upload": needs_upload,
            "action": action,
        },
    }


@app.get(
    "/api/v1/jobs/{job_id}/completion-email",
    dependencies=[Depends(verify_api_key)],
    tags=["Delivery"],
)
def get_job_completion_email(job_id: str, db: Session = Depends(get_db)):
    """
    Fetch the freshly formatted completion email for a job after all Drive artifacts have been uploaded.
    """
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    req_types = get_required_artifact_types(job)
    attr_map = {
        "audio": job.drive_file_id,
        "details": job.details_drive_file_id,
    }
    missing = [t for t in req_types if not attr_map.get(t)]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot generate completion email: missing required Drive artifact IDs ({', '.join(missing)})",
        )

    script = job.script_json or {}
    segments = script.get("segments", [])
    warnings = script.get("warnings", [])

    created_iso = job.created_at.isoformat() if job.created_at else ""
    completed_iso = job.completed_at.isoformat() if job.completed_at else None
    dur_info = calculate_script_duration(script, job.kokoro_speed or job.custom_speed or 1.0)

    formatted_email = format_completion_email(
        job_id=job.id,
        episode_title=job.custom_title or script.get("episode_title", "Herald Episode"),
        episode_description=script.get("episode_description", ""),
        drive_web_link=job.drive_web_link,
        duration_seconds=job.audio_duration_seconds or dur_info["predicted_duration_seconds"],
        file_bytes=job.audio_bytes or 0,
        request_mode=job.request_mode,
        source_type=job.source_type,
        source_title=job.custom_title or script.get("episode_title"),
        script_estimated_minutes=float(dur_info["estimated_minutes"]),
        segments_count=len(segments),
        sha256=job.audio_sha256 or "",
        chunk_count=job.completed_chunk_index or 0,
        retry_attempts=max(0, job.attempt_count or 0),
        drive_file_id=job.drive_file_id,
        details_drive_link=job.details_drive_web_link,
        details_drive_id=job.details_drive_file_id,
        source_drive_link=job.source_drive_web_link,
        source_drive_id=job.source_drive_file_id,
        diagnostics_drive_link=job.diagnostics_drive_web_link,
        diagnostics_drive_id=job.diagnostics_drive_file_id,
        created_at_iso=created_iso,
        completed_at_iso=completed_iso,
        gemini_model=job.gemini_model or "gemini-3.5-flash",
        kokoro_voice=job.kokoro_voice or job.custom_voice or "af_heart",
        kokoro_speed=job.kokoro_speed or job.custom_speed or 1.0,
        script_warnings=warnings,
        research_notes_drive_link=job.research_notes_drive_web_link,
        research_notes_drive_id=job.research_notes_drive_file_id,
        verify_enabled=bool(job.verify_final_script),
    )

    ep_title = job.custom_title or script.get("episode_title") or "Herald Episode"
    audio_fn = build_user_facing_drive_filename(ep_title, job.created_at, job.request_mode, "mp3")
    details_fn = build_user_facing_drive_filename(ep_title, job.created_at, job.request_mode, "md")

    return {
        "job_id": job.id,
        "status": job.status,
        "text": formatted_email["text"],
        "html": formatted_email["html"],
        "audio_drive_filename": audio_fn,
        "details_drive_filename": details_fn,
    }


@app.post(
    "/api/v1/jobs/{job_id}/drive-complete",
    dependencies=[Depends(verify_api_key)],
    tags=["Delivery"],
)
def update_drive_complete(
    job_id: str, req: DriveCompleteRequest, db: Session = Depends(get_db)
):
    """Record Google Drive file IDs and web links independently and idempotently."""
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status == JobState.COMPLETE.value:
        if (req.drive_file_id and job.drive_file_id and job.drive_file_id != req.drive_file_id) or \
           (req.details_drive_file_id and job.details_drive_file_id and job.details_drive_file_id != req.details_drive_file_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conflicting Drive file ID on COMPLETE job: existing vs new",
            )
        return {
            "job_id": job.id,
            "status": job.status,
            "drive_file_id": job.drive_file_id,
            "drive_web_link": job.drive_web_link,
            "details_drive_file_id": job.details_drive_file_id,
            "details_drive_web_link": job.details_drive_web_link,
            "message": "Job already COMPLETE.",
        }

    updated_any = False

    t_start = normalize_gmail_timestamp(req.started_at) or datetime.now(UTC)
    t_finish = normalize_gmail_timestamp(req.finished_at) or datetime.now(UTC)
    dur_ms = req.duration_ms
    if dur_ms is None and t_start and t_finish:
        dur_ms = max(0, int((t_finish - t_start).total_seconds() * 1000))

    is_audio = req.artifact_type == "audio" or req.drive_file_id
    is_details = req.artifact_type == "details" or req.details_drive_file_id

    if is_audio and req.drive_file_id:
        if job.drive_file_id and job.drive_file_id != req.drive_file_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Conflicting audio Drive file ID: existing '{job.drive_file_id}' vs new '{req.drive_file_id}'",
            )
        job.drive_file_id = req.drive_file_id
        if req.drive_web_link:
            job.drive_web_link = req.drive_web_link
        updated_any = True

    if is_details and req.details_drive_file_id:
        if job.details_drive_file_id and job.details_drive_file_id != req.details_drive_file_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Conflicting details Drive file ID: existing '{job.details_drive_file_id}' vs new '{req.details_drive_file_id}'",
            )
        job.details_drive_file_id = req.details_drive_file_id
        if req.details_drive_web_link:
            job.details_drive_web_link = req.details_drive_web_link
        updated_any = True

    # Legacy field handling for backwards compatibility
    if req.source_drive_file_id:
        job.source_drive_file_id = req.source_drive_file_id
        if req.source_drive_web_link:
            job.source_drive_web_link = req.source_drive_web_link
        updated_any = True
    if req.script_drive_file_id:
        job.script_drive_file_id = req.script_drive_file_id
        if req.script_drive_web_link:
            job.script_drive_web_link = req.script_drive_web_link
        updated_any = True
    if req.diagnostics_drive_file_id:
        job.diagnostics_drive_file_id = req.diagnostics_drive_file_id
        if req.diagnostics_drive_web_link:
            job.diagnostics_drive_web_link = req.diagnostics_drive_web_link
        updated_any = True
    if req.research_drive_file_id:
        job.research_drive_file_id = req.research_drive_file_id
        if req.research_drive_web_link:
            job.research_drive_web_link = req.research_drive_web_link
        updated_any = True
    if req.research_notes_drive_file_id:
        job.research_notes_drive_file_id = req.research_notes_drive_file_id
        if req.research_notes_drive_web_link:
            job.research_notes_drive_web_link = req.research_notes_drive_web_link
        updated_any = True

    if req.drive_job_key:
        job.drive_job_key = req.drive_job_key

    if updated_any:
        job.drive_uploaded_at = datetime.now(UTC)

    if job.status != JobState.DELIVERING.value and (
        job.drive_file_id or job.details_drive_file_id
    ):
        transition_job_state(db, job, JobState.DELIVERING.value, component="n8n-drive-complete", commit=False)

    db.commit()
    db.refresh(job)

    # Record telemetry AFTER db.commit() to release row lock
    if is_audio:
        record_stage_metric(
            job_id=job.id,
            stage="DRIVE_AUDIO_UPLOAD",
            started_at=t_start,
            finished_at=t_finish,
            duration_ms=dur_ms,
            status="success",
            output_bytes=job.audio_bytes,
        )

        output_dir = Path(settings.HERALD_WORK_DIR) / "output"
        try:
            ensure_details_artifact(job, output_dir, db=db)
        except Exception as de:
            logger.warning(f"Could not regenerate details artifact after audio upload: {de}")

    elif is_details:
        record_stage_metric(
            job_id=job.id,
            stage="DRIVE_DETAILS_UPLOAD",
            started_at=t_start,
            finished_at=t_finish,
            duration_ms=dur_ms,
            status="success",
        )

    return {
        "job_id": job.id,
        "status": job.status,
        "drive_file_id": job.drive_file_id,
        "drive_web_link": job.drive_web_link,
        "details_drive_file_id": job.details_drive_file_id,
        "details_drive_web_link": job.details_drive_web_link,
    }


@app.post(
    "/api/v1/jobs/{job_id}/delivery-complete",
    dependencies=[Depends(verify_api_key)],
    tags=["Delivery"],
)
def update_delivery_complete(
    job_id: str, req: DeliveryCompleteRequest | None = None, db: Session = Depends(get_db)
):
    """Record successful Gmail delivery and transition job to COMPLETE. Requires mode-appropriate Drive artifact IDs."""
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    new_msg_id = req.gmail_result_message_id if req else None

    if job.status == JobState.COMPLETE.value:
        if new_msg_id and job.gmail_result_message_id and job.gmail_result_message_id != new_msg_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Conflicting Gmail result message ID on COMPLETE job: existing '{job.gmail_result_message_id}' vs new '{new_msg_id}'",
            )
        return {
            "job_id": job.id,
            "status": job.status,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "message": "Job already COMPLETE.",
        }

    # Ensure required Drive artifacts exist before transitioning to COMPLETE
    req_types = get_required_artifact_types(job)
    attr_map = {
        "audio": job.drive_file_id,
        "details": job.details_drive_file_id,
    }
    missing = [t for t in req_types if not attr_map.get(t)]

    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot transition job to COMPLETE: missing required Drive artifact IDs ({', '.join(missing)})",
        )

    if job.gmail_result_message_id and new_msg_id and job.gmail_result_message_id != new_msg_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Conflicting Gmail result message ID: existing '{job.gmail_result_message_id}' vs new '{new_msg_id}'",
        )

    if new_msg_id:
        job.gmail_result_message_id = new_msg_id

    now = datetime.now(UTC)
    if not job.delivered_at:
        job.delivered_at = now
    if not job.completed_at:
        job.completed_at = now

    db.commit()

    if job.status != JobState.COMPLETE.value:
        transition_job_state(db, job, JobState.COMPLETE.value, component="n8n-delivery-complete")

    # Record delivery and end-to-end metrics
    t_start = normalize_gmail_timestamp(req.started_at) if req else None
    t_finish = normalize_gmail_timestamp(req.finished_at) if req else None
    dur_ms = req.duration_ms if req else None
    if dur_ms is None and t_start and t_finish:
        dur_ms = max(0, int((t_finish - t_start).total_seconds() * 1000))

    record_stage_metric(
        job_id=job.id,
        stage="EMAIL_DELIVERY",
        started_at=t_start or now,
        finished_at=t_finish or now,
        duration_ms=dur_ms,
        status="success",
    )

    if job.audio_ready_at:
        deliv_tot_ms = max(0, int((now - ensure_utc(job.audio_ready_at)).total_seconds() * 1000))
        record_stage_metric(
            job_id=job.id,
            stage="DELIVERY_TOTAL",
            started_at=job.audio_ready_at,
            finished_at=now,
            duration_ms=deliv_tot_ms,
            status="success",
        )

    e2e_start = ensure_utc(job.gmail_received_at) or ensure_utc(job.created_at)
    if e2e_start:
        e2e_ms = max(0, int((now - e2e_start).total_seconds() * 1000))
        baseline_str = "gmail_received_at" if job.gmail_received_at else "created_at"
        record_stage_metric(
            job_id=job.id,
            stage="END_TO_END",
            started_at=e2e_start,
            finished_at=now,
            duration_ms=e2e_ms,
            status="success",
            metadata_json={"baseline": baseline_str},
        )

    # Regenerate local details Markdown report from final COMPLETE database state
    output_dir = Path(settings.HERALD_WORK_DIR) / "output"
    try:
        ensure_details_artifact(job, output_dir)
    except Exception:
        pass

    return {
        "job_id": job.id,
        "status": job.status,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


class DetailsFinalizedRequest(BaseModel):
    details_drive_file_id: str | None = Field(None, description="Final Drive file ID for updated Details Markdown")
    details_drive_web_link: str | None = Field(None, description="Final Drive web link for updated Details Markdown")
    started_at: Any | None = Field(None, description="Details finalize start timestamp")
    finished_at: Any | None = Field(None, description="Details finalize finish timestamp")
    duration_ms: int | None = Field(None, description="Details finalize duration in milliseconds")


@app.post(
    "/api/v1/jobs/{job_id}/details-finalized",
    dependencies=[Depends(verify_api_key)],
    tags=["Delivery"],
)
def update_details_finalized(
    job_id: str, req: DetailsFinalizedRequest | None = None, db: Session = Depends(get_db)
):
    """
    Record in-place Google Drive update for companion details artifact after job reaches COMPLETE.
    Persists details_finalized_at and records stage metric DRIVE_DETAILS_FINALIZE.
    """
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    now = datetime.now(UTC)
    if req and req.details_drive_file_id:
        job.details_drive_file_id = req.details_drive_file_id
    if req and req.details_drive_web_link:
        job.details_drive_web_link = req.details_drive_web_link

    job.details_finalized_at = now
    db.commit()

    t_start = normalize_gmail_timestamp(req.started_at) if req else None
    t_finish = normalize_gmail_timestamp(req.finished_at) if req else None
    dur_ms = req.duration_ms if req else None
    if dur_ms is None and t_start and t_finish:
        dur_ms = max(0, int((t_finish - t_start).total_seconds() * 1000))

    record_stage_metric(
        job_id=job.id,
        stage="DRIVE_DETAILS_FINALIZE",
        started_at=t_start or now,
        finished_at=t_finish or now,
        duration_ms=dur_ms,
        status="success",
    )

    return {
        "job_id": job.id,
        "status": job.status,
        "details_drive_file_id": job.details_drive_file_id,
        "details_drive_web_link": job.details_drive_web_link,
        "details_finalized_at": job.details_finalized_at.isoformat() if job.details_finalized_at else None,
    }


class DeliveryNudgeRequest(BaseModel):
    job_id: str = Field(..., description="Job ID to nudge delivery for")
    event: str | None = Field(default="AUDIO_READY")


@app.post(
    "/api/v1/delivery/nudge",
    dependencies=[Depends(verify_api_key)],
    tags=["Delivery"],
)
def delivery_nudge_endpoint(req: DeliveryNudgeRequest, db: Session = Depends(get_db)):
    """
    Public API delivery nudge proxy endpoint. Authenticates external API clients and forwards
    the delivery nudge to the internal n8n webhook (/webhook/herald-audio-ready).
    """
    job = db.query(PodcastJob).filter(PodcastJob.id == req.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    nudge_url = getattr(settings, "DELIVERY_NUDGE_WEBHOOK_URL", "http://n8n:5678/webhook/herald-audio-ready")
    nudge_secret = getattr(settings, "DELIVERY_NUDGE_SECRET", "") or settings.HERALD_API_KEY
    nudge_timeout = getattr(settings, "DELIVERY_NUDGE_TIMEOUT_SECONDS", 3.0)

    webhook_dispatched = False
    try:
        headers = {"Content-Type": "application/json"}
        if nudge_secret:
            headers["X-API-Key"] = nudge_secret
            headers["X-Herald-Delivery-Token"] = nudge_secret
        with httpx.Client(timeout=nudge_timeout) as client:
            client.post(nudge_url, json={"job_id": job.id, "event": req.event or "AUDIO_READY"}, headers=headers)
        webhook_dispatched = True
    except Exception as ne:
        logger.warning(f"Delivery nudge proxy for job '{job.id}' to n8n webhook failed non-fatally: {ne}")

    return {
        "nudged": True,
        "job_id": job.id,
        "status": job.status,
        "webhook_dispatched": webhook_dispatched,
        "message": "Delivery nudge proxy received and dispatched to n8n webhook.",
    }



@app.post(
    "/api/v1/jobs/{job_id}/delivery-failed",
    dependencies=[Depends(verify_api_key)],
    tags=["Delivery"],
)
def update_delivery_failed(
    job_id: str, req: DeliveryFailedRequest, db: Session = Depends(get_db)
):
    """Record delivery failure with bounded backoff and transition to FAILED_RETRYABLE or FAILED_FINAL."""
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status == JobState.COMPLETE.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="COMPLETE job cannot be set to failed.")

    job.delivery_attempt_count += 1
    job.attempt_count += 1

    delay_seconds = min(30 * (2 ** (job.delivery_attempt_count - 1)), 3600)
    job.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)

    target_failed_state = (
        JobState.FAILED_FINAL.value if job.delivery_attempt_count >= 3 else JobState.FAILED_RETRYABLE.value
    )

    transition_job_state(
        db,
        job,
        target_failed_state,
        component="n8n-delivery-failed",
        message=req.error_detail[:500],
        error_category=req.error_code,
    )

    return {
        "job_id": job.id,
        "status": job.status,
        "failed_stage": job.failed_stage,
        "delivery_attempt_count": job.delivery_attempt_count,
        "next_retry_at": job.next_retry_at.isoformat() if job.next_retry_at else None,
    }


@app.get(
    "/api/v1/jobs/{job_id}",
    response_model=JobStatusResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["Jobs"],
)
def get_job_status(job_id: str, db: Session = Depends(get_db)):
    """Get detailed status for a podcast job."""
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobStatusResponse(
        id=job.id,
        gmail_message_id=job.gmail_message_id,
        gmail_thread_id=job.gmail_thread_id,
        sender_email=job.sender_email,
        request_mode=job.request_mode,
        research_depth=job.research_depth,
        source_type=job.source_type,
        source_url=job.source_url,
        status=job.status,
        attempt_count=job.attempt_count,
        synthesis_attempt_count=job.synthesis_attempt_count,
        delivery_attempt_count=job.delivery_attempt_count,
        completed_chunk_index=job.completed_chunk_index,
        local_audio_path=job.local_audio_path,
        audio_bytes=job.audio_bytes,
        audio_sha256=job.audio_sha256,
        audio_duration_seconds=job.audio_duration_seconds,
        drive_file_id=job.drive_file_id,
        drive_web_link=job.drive_web_link,
        details_drive_file_id=job.details_drive_file_id,
        details_drive_web_link=job.details_drive_web_link,
        source_drive_file_id=job.source_drive_file_id,
        source_drive_web_link=job.source_drive_web_link,
        script_drive_file_id=job.script_drive_file_id,
        script_drive_web_link=job.script_drive_web_link,
        diagnostics_drive_file_id=job.diagnostics_drive_file_id,
        diagnostics_drive_web_link=job.diagnostics_drive_web_link,
        research_drive_file_id=job.research_drive_file_id,
        research_drive_web_link=job.research_drive_web_link,
        research_notes_drive_file_id=job.research_notes_drive_file_id,
        research_notes_drive_web_link=job.research_notes_drive_web_link,
        drive_job_key=job.drive_job_key,
        gmail_result_message_id=job.gmail_result_message_id,
        kokoro_voice=job.kokoro_voice,
        kokoro_speed=job.kokoro_speed,
        gemini_model=job.gemini_model,
        research_model=job.research_model,
        research_search_count=job.research_search_count,
        research_source_count=job.research_source_count,
        research_repair_count=job.research_repair_count,
        error_code=job.error_code,
        error_detail=job.error_detail,
        created_at=job.created_at.isoformat() if job.created_at else "",
        updated_at=job.updated_at.isoformat() if job.updated_at else "",
        gmail_received_at=job.gmail_received_at.isoformat() if job.gmail_received_at else None,
        audio_ready_at=job.audio_ready_at.isoformat() if job.audio_ready_at else None,
        drive_uploaded_at=job.drive_uploaded_at.isoformat() if job.drive_uploaded_at else None,
        delivered_at=job.delivered_at.isoformat() if job.delivered_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )


@app.get(
    "/api/v1/jobs/{job_id}/performance",
    dependencies=[Depends(verify_api_key)],
    tags=["Performance"],
)
def get_job_performance(job_id: str, db: Session = Depends(get_db)):
    """Return structured per-stage timing performance metrics and Kokoro summary for a job."""
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    metrics = (
        db.query(JobProcessingMetric)
        .filter(JobProcessingMetric.job_id == job_id)
        .order_by(JobProcessingMetric.started_at.asc(), JobProcessingMetric.created_at.asc())
        .all()
    )

    raw_stages = []
    stage_totals: dict[str, int] = {}
    kokoro_requests = []

    for m in metrics:
        stage_item = {
            "id": m.id,
            "stage": m.stage,
            "substage": m.substage,
            "attempt": m.attempt,
            "sequence_index": m.sequence_index,
            "started_at": m.started_at.isoformat() if m.started_at else None,
            "finished_at": m.finished_at.isoformat() if m.finished_at else None,
            "duration_ms": m.duration_ms,
            "status": m.status,
            "input_chars": m.input_chars,
            "output_bytes": m.output_bytes,
            "audio_duration_ms": m.audio_duration_ms,
            "metadata": m.metadata_json or {},
        }
        raw_stages.append(stage_item)

        if m.stage == "KOKORO_REQUEST":
            kokoro_requests.append(m)
        elif m.duration_ms is not None:
            stage_totals[m.stage] = m.duration_ms

    total_kokoro_requests = len(kokoro_requests)
    successful_kokoro = [k for k in kokoro_requests if k.status == "success"]
    failed_kokoro = [k for k in kokoro_requests if k.status != "success"]

    total_input_chars = sum(k.input_chars or 0 for k in kokoro_requests)
    total_wall_time_ms = sum(k.duration_ms or 0 for k in successful_kokoro)
    total_audio_duration_ms = sum(k.audio_duration_ms or 0 for k in successful_kokoro)

    # Requirement 7: Weighted RTF = sum(successful wall time) / sum(successful audio duration)
    kokoro_rtf = None
    if total_audio_duration_ms > 0 and total_wall_time_ms > 0:
        kokoro_rtf = round(total_wall_time_ms / total_audio_duration_ms, 3)

    end_to_end_ms = stage_totals.get("END_TO_END")
    if end_to_end_ms is None and job.created_at and job.completed_at:
        end_to_end_ms = int((ensure_utc(job.completed_at) - ensure_utc(job.created_at)).total_seconds() * 1000)

    return {
        "job_id": job.id,
        "status": job.status,
        "request_mode": job.request_mode,
        "gmail_received_at": job.gmail_received_at.isoformat() if job.gmail_received_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "end_to_end_ms": end_to_end_ms,
        "tts_queue_wait_ms": stage_totals.get("TTS_QUEUE_WAIT"),
        "delivery_dispatch_wait_ms": stage_totals.get("DELIVERY_DISPATCH_WAIT"),
        "stages": stage_totals,
        "raw_stage_metrics": raw_stages,
        "kokoro": {
            "requests": total_kokoro_requests,
            "successful_requests": len(successful_kokoro),
            "failed_attempts": len(failed_kokoro),
            "input_chars": total_input_chars,
            "wall_time_ms": total_wall_time_ms,
            "audio_duration_ms": total_audio_duration_ms,
            "rtf": kokoro_rtf,
        },
    }


def _calculate_series_stats(values: list[float | int]) -> dict[str, Any]:
    """Calculate count, mean, median, p90, p95, min, max without external dependencies."""
    if not values:
        return {"count": 0, "mean": None, "median": None, "p90": None, "p95": None, "min": None, "max": None}

    s_vals = sorted(values)
    n = len(s_vals)
    mean_val = sum(s_vals) / float(n)

    if n % 2 == 1:
        median_val = float(s_vals[n // 2])
    else:
        median_val = (s_vals[n // 2 - 1] + s_vals[n // 2]) / 2.0

    def _p_calc(p: float) -> float:
        if n == 1:
            return float(s_vals[0])
        idx = p * (n - 1)
        lower = int(idx)
        upper = min(lower + 1, n - 1)
        w = idx - lower
        return float(s_vals[lower] * (1.0 - w) + s_vals[upper] * w)

    return {
        "count": n,
        "mean": round(mean_val, 2),
        "median": round(median_val, 2),
        "p90": round(_p_calc(0.90), 2),
        "p95": round(_p_calc(0.95), 2),
        "min": float(s_vals[0]),
        "max": float(s_vals[-1]),
    }


@app.get(
    "/api/v1/ops/performance",
    dependencies=[Depends(verify_api_key)],
    tags=["Operations"],
)
def get_ops_performance(limit: int = 50, db: Session = Depends(get_db)):
    """Return aggregate performance statistics across recent completed podcast jobs."""
    recent_jobs = (
        db.query(PodcastJob)
        .order_by(PodcastJob.created_at.desc())
        .limit(limit)
        .all()
    )

    job_ids = [j.id for j in recent_jobs]
    if not job_ids:
        return {
            "window_size": 0,
            "jobs_evaluated": 0,
            "stages": {},
            "kokoro_summary": {"avg_requests_per_episode": 0, "total_failed_retried_attempts": 0},
            "by_request_mode": {},
        }

    metrics = (
        db.query(JobProcessingMetric)
        .filter(JobProcessingMetric.job_id.in_(job_ids))
        .all()
    )

    # Group metrics by stage and by job
    stage_durations: dict[str, list[int]] = {}
    job_metrics_map: dict[str, list[JobProcessingMetric]] = {j_id: [] for j_id in job_ids}
    for m in metrics:
        job_metrics_map.setdefault(m.job_id, []).append(m)
        if m.stage != "KOKORO_REQUEST" and m.duration_ms is not None:
            stage_durations.setdefault(m.stage, []).append(m.duration_ms)

    # Calculate aggregate RTF per job and stats
    job_rtfs: list[float] = []
    total_kokoro_req_count = 0
    total_failed_kokoro_attempts = 0

    for j_id, j_metrics in job_metrics_map.items():
        k_reqs = [m for m in j_metrics if m.stage == "KOKORO_REQUEST"]
        total_kokoro_req_count += len(k_reqs)
        total_failed_kokoro_attempts += len([m for m in k_reqs if m.status != "success"])

        succ_k = [m for m in k_reqs if m.status == "success"]
        wall_ms = sum(m.duration_ms or 0 for m in succ_k)
        aud_ms = sum(m.audio_duration_ms or 0 for m in succ_k)
        if aud_ms > 0 and wall_ms > 0:
            job_rtfs.append(round(wall_ms / float(aud_ms), 3))

    stage_stats = {stg: _calculate_series_stats(vals) for stg, vals in stage_durations.items()}
    stage_stats["KOKORO_RTF"] = _calculate_series_stats(job_rtfs)

    avg_k_reqs = round(total_kokoro_req_count / float(len(job_ids)), 2) if job_ids else 0.0

    # Grouping by request mode
    mode_counts: dict[str, int] = {}
    for j in recent_jobs:
        m_str = (j.request_mode or "standard").lower()
        mode_counts[m_str] = mode_counts.get(m_str, 0) + 1

    return {
        "window_size": limit,
        "jobs_evaluated": len(recent_jobs),
        "stages": stage_stats,
        "kokoro_summary": {
            "avg_requests_per_episode": avg_k_reqs,
            "total_failed_retried_attempts": total_failed_kokoro_attempts,
        },
        "by_request_mode": mode_counts,
    }



@app.post(
    "/api/v1/jobs/{job_id}/retry",
    dependencies=[Depends(verify_api_key)],
    tags=["Jobs"],
)
def retry_job(job_id: str, db: Session = Depends(get_db)):
    """
    Executable stage-specific retry endpoint.
    Determines exact resume state based on failed_stage and persisted artifacts without force=True.
    """
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status not in (JobState.FAILED_RETRYABLE.value, JobState.FAILED_FINAL.value):
        raise HTTPException(status_code=400, detail=f"Job is in state '{job.status}', not FAILED")

    if job.attempt_count >= 3:
        transition_job_state(
            db,
            job,
            JobState.FAILED_FINAL.value,
            component="herald-retry",
            message="Maximum retry attempt limit reached",
            commit=True,
        )
        raise HTTPException(status_code=400, detail="Job has reached maximum retry attempt limit (FAILED_FINAL).")

    failed_stage = job.failed_stage or "QUEUED_TTS"

    if failed_stage == JobState.EXTRACTING.value:
        target_state = JobState.EXTRACTING.value
    elif failed_stage == JobState.SCRIPTING.value:
        target_state = JobState.SCRIPTING.value
    elif failed_stage in (JobState.SYNTHESIZING.value, JobState.ENCODING.value, JobState.QUEUED_TTS.value):
        target_state = JobState.QUEUED_TTS.value
    elif failed_stage == JobState.UPLOADING.value:
        target_state = JobState.DELIVERING.value if (job.drive_file_id and job.drive_web_link) else JobState.UPLOADING.value
    elif failed_stage == JobState.DELIVERING.value:
        target_state = JobState.DELIVERING.value
    else:
        target_state = JobState.QUEUED_TTS.value

    job.attempt_count += 1
    job.error_code = None
    job.error_detail = None
    job.next_retry_at = None
    db.commit()

    transition_job_state(
        db, job, target_state, component="herald-api-retry", message=f"Resuming retry from failed stage '{failed_stage}' to '{target_state}'", force=False
    )
    return {
        "job_id": job.id,
        "status": job.status,
        "resumed_from_stage": failed_stage,
        "target_stage": target_state,
        "attempt_count": job.attempt_count,
    }


# =====================================================================
# Operations API Endpoints (called by n8n operational workflows)
# =====================================================================

@app.post(
    "/api/v1/ops/cleanup",
    dependencies=[Depends(verify_api_key)],
    tags=["Operations"],
)
def ops_daily_cleanup(db: Session = Depends(get_db)):
    """
    Daily cleanup: Delete local MP3 & intermediate chunks for COMPLETE jobs > 48 hours old.
    Never deletes Google Drive files.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=48)
    eligible_jobs = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.status == JobState.COMPLETE.value,
            PodcastJob.completed_at < cutoff,
            PodcastJob.local_audio_path.isnot(None),
        )
        .all()
    )

    cleaned_count = 0
    freed_bytes = 0
    work_dir = Path(settings.HERALD_WORK_DIR)

    for job in eligible_jobs:
        names = get_artifact_filenames(job)
        if job.local_audio_path:
            p = Path(job.local_audio_path)
            if p.exists():
                freed_bytes += p.stat().st_size
                p.unlink(missing_ok=True)
            job.local_audio_path = None
            cleaned_count += 1

        output_dir = work_dir / "output"
        for k in ("details_filename", "source_filename", "script_filename", "diagnostics_filename", "research_filename", "research_notes_filename"):
            fname = names.get(k)
            if fname:
                art_p = output_dir / fname
                if art_p.exists():
                    freed_bytes += art_p.stat().st_size
                    art_p.unlink(missing_ok=True)

        chunks_dir = work_dir / "jobs" / job.id
        if chunks_dir.exists():
            shutil.rmtree(chunks_dir, ignore_errors=True)

    db.commit()

    return {
        "status": "success",
        "cleaned_jobs_count": cleaned_count,
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
    }


@app.post(
    "/api/v1/ops/stale-recovery",
    dependencies=[Depends(verify_api_key)],
    tags=["Operations"],
)
def ops_stale_recovery(db: Session = Depends(get_db)):
    """
    Recover stale jobs across all active stages using stage-specific timeouts and conditional updates.
    """
    now = datetime.now(UTC)
    recovered_count = 0

    stale_specs = [
        (JobState.EXTRACTING.value, timedelta(minutes=15), JobState.EXTRACTING.value),
        (JobState.SCRIPTING.value, timedelta(minutes=15), JobState.SCRIPTING.value),
        (JobState.SYNTHESIZING.value, timedelta(minutes=15), JobState.QUEUED_TTS.value),
        (JobState.ENCODING.value, timedelta(minutes=15), JobState.QUEUED_TTS.value),
        (JobState.UPLOADING.value, timedelta(minutes=30), JobState.UPLOADING.value),
        (JobState.DELIVERING.value, timedelta(minutes=30), JobState.DELIVERING.value),
    ]

    for status_val, timeout, target_val in stale_specs:
        cutoff = now - timeout
        jobs = (
            db.query(PodcastJob)
            .filter(PodcastJob.status == status_val)
            .with_for_update(skip_locked=True)
            .all()
        )
        for job in jobs:
            last_active = job.last_heartbeat_at or job.claimed_at
            if last_active:
                if last_active.tzinfo is None:
                    last_active = last_active.replace(tzinfo=UTC)
                if last_active < cutoff:
                    job.claimed_at = None
                    job.claim_owner = None
                    job.last_heartbeat_at = None
                    target_state = (
                        JobState.FAILED_FINAL.value if job.attempt_count >= 3 else target_val
                    )
                    transition_job_state(
                        db,
                        job,
                        target_state,
                        component="herald-ops-stale-recovery",
                        message=f"Recovered stale claim in state '{status_val}' after timeout",
                        force=True,
                        commit=False,
                    )
                    recovered_count += 1

    db.commit()
    return {"status": "success", "recovered_jobs": recovered_count}


@app.get(
    "/api/v1/ops/daily-health",
    dependencies=[Depends(verify_api_key)],
    tags=["Operations"],
)
def ops_daily_health(db: Session = Depends(get_db)):
    """
    Generate daily health metrics report: queue counts, failures, oldest waiting job, longest active job, free disk, database size, work dir.
    """
    counts = {}
    for st in JobState:
        cnt = db.query(PodcastJob).filter(PodcastJob.status == st.value).count()
        counts[st.value] = cnt

    past_24h = datetime.now(UTC) - timedelta(hours=24)
    failures_24h = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.status.in_([JobState.FAILED_RETRYABLE.value, JobState.FAILED_FINAL.value]),
            PodcastJob.updated_at >= past_24h,
        )
        .count()
    )

    oldest_pending = (
        db.query(PodcastJob)
        .filter(PodcastJob.status.notin_([JobState.COMPLETE.value, JobState.FAILED_FINAL.value, JobState.CANCELLED.value]))
        .order_by(PodcastJob.created_at.asc())
        .first()
    )

    work_dir = Path(settings.HERALD_WORK_DIR)
    try:
        free_mb = check_free_disk_mb(work_dir)
    except Exception:
        free_mb = 0.0

    completed_uploads = (
        db.query(PodcastJob)
        .filter(PodcastJob.drive_file_id.isnot(None))
        .count()
    )

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "queue_counts": counts,
        "failures_past_24h": failures_24h,
        "oldest_pending_job_id": oldest_pending.id if oldest_pending else None,
        "oldest_pending_created_at": oldest_pending.created_at.isoformat() if oldest_pending else None,
        "free_disk_mb": free_mb,
        "completed_uploads_total": completed_uploads,
        "readiness_status": "OK" if free_mb >= settings.HERALD_MIN_DISK_MB else "DEGRADED",
    }


@app.post(
    "/api/v1/ops/weekly-maintenance",
    dependencies=[Depends(verify_api_key)],
    tags=["Operations"],
)
def ops_weekly_maintenance(db: Session = Depends(get_db)):
    """
    Weekly maintenance inspection: orphaned file audit, database stats, disk usage overview.
    """
    work_dir = Path(settings.HERALD_WORK_DIR)
    jobs_dir = work_dir / "jobs"
    orphan_dirs_count = 0

    if jobs_dir.exists():
        for item in jobs_dir.iterdir():
            if item.is_dir():
                job_exists = db.query(PodcastJob).filter(PodcastJob.id == item.name).first()
                if not job_exists:
                    shutil.rmtree(item, ignore_errors=True)
                    orphan_dirs_count += 1

    total_jobs = db.query(PodcastJob).count()
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "total_jobs_in_db": total_jobs,
        "orphaned_dirs_cleaned": orphan_dirs_count,
        "status": "completed",
    }


class ErrorHandlerRequest(BaseModel):
    job_id: str | None = None
    error_code: str = "WORKFLOW_EXECUTION_ERROR"
    error_detail: str = "An error occurred in n8n execution workflow"
    failed_stage: str | None = None


@app.post(
    "/api/v1/ops/error-handler",
    dependencies=[Depends(verify_api_key)],
    tags=["Operations"],
)
def ops_error_handler(req: ErrorHandlerRequest, db: Session = Depends(get_db)):
    """
    Error handler: updates durable job state when job_id is available and returns sanitized alert.
    """
    if req.job_id:
        job = db.query(PodcastJob).filter(PodcastJob.id == req.job_id).first()
        if job and job.status != JobState.COMPLETE.value:
            transition_job_state(
                db,
                job,
                JobState.FAILED_RETRYABLE.value,
                component="n8n-error-handler",
                message=req.error_detail[:500],
                error_category=req.error_code,
            )

    sanitized_alert = {
        "alert": "HERALD_SYSTEM_ERROR",
        "job_id": req.job_id,
        "error_code": req.error_code,
        "error_detail": req.error_detail[:200],
        "failed_stage": req.failed_stage,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    return sanitized_alert
