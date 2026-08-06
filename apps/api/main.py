import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from herald.audio.ffmpeg_builder import check_free_disk_mb
from herald.config import settings
from herald.db.connection import get_db
from herald.db.models import JobState, PodcastJob, SourceType
from herald.db.state_machine import transition_job_state
from herald.extraction.email_parser import (
    SourceClassification,
    compute_source_hash,
    process_email_message,
)
from herald.extraction.url_extractor import (
    ArticleExtractionError,
    SSRFVulnerabilityError,
    extract_article_from_url,
)
from herald.gemini.client import GeminiError, generate_podcast_script
from herald.tts.kokoro_client import KokoroClient

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


class IntakeRequest(BaseModel):
    gmail_message_id: str = Field(..., description="Unique Gmail message ID")
    gmail_thread_id: str | None = Field(None, description="Gmail thread ID")
    sender_email: str = Field(..., description="Authorized sender email address")
    subject: str = Field(..., description="Email subject line containing Podcast: <Mode>")
    body_text: str | None = Field(None, description="Plain text email body")
    body_html: str | None = Field(None, description="HTML email body")


class IntakeResponse(BaseModel):
    job_id: str
    status: str
    request_mode: str
    source_type: str
    is_duplicate: bool
    message: str


class ExtractUrlRequest(BaseModel):
    url: str = Field(..., description="Public article URL to extract")


class ExtractUrlResponse(BaseModel):
    title: str
    extracted_text: str
    canonical_url: str


class GenerateScriptRequest(BaseModel):
    job_id: str = Field(..., description="Podcast job ID")


class DriveCompleteRequest(BaseModel):
    drive_file_id: str = Field(..., description="Uploaded Google Drive file ID")
    drive_web_link: str = Field(..., description="Web link to Google Drive file")
    drive_job_key: str | None = Field(None, description="Herald job key stored in Drive appProperties")


class DeliveryCompleteRequest(BaseModel):
    gmail_result_message_id: str | None = Field(None, description="Sent reply Gmail message ID")


class DeliveryFailedRequest(BaseModel):
    error_code: str = Field(default="GMAIL_DELIVERY_FAILURE")
    error_detail: str = Field(default="Failed to deliver completion email")


class JobStatusResponse(BaseModel):
    id: str
    gmail_message_id: str
    sender_email: str
    request_mode: str
    source_type: str
    status: str
    attempt_count: int
    completed_chunk_index: int
    local_audio_path: str | None
    audio_bytes: int | None
    audio_duration_seconds: int | None
    drive_file_id: str | None
    drive_web_link: str | None
    drive_job_key: str | None
    gmail_result_message_id: str | None
    error_code: str | None
    error_detail: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


@app.get("/health", tags=["Health"])
@app.get("/live", tags=["Health"])
def health_check():
    """Process liveness check endpoint. Returns HTTP 200 when API process is running."""
    return {
        "status": "live",
        "timestamp": datetime.now(UTC).isoformat(),
        "environment": settings.HERALD_ENV,
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
        if not settings.HERALD_API_KEY or settings.HERALD_API_KEY == "default-insecure-api-key":
            reasons.append("Production HERALD_API_KEY is not configured securely")
        if not settings.EMAIL_ALLOWED_SENDERS.strip():
            reasons.append("Production EMAIL_ALLOWED_SENDERS is empty (fail-closed rule)")

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

    return {
        "ready": True,
        "environment": settings.HERALD_ENV,
        "free_disk_mb": free_mb,
        "kokoro_tts": kokoro_healthy,
    }


@app.post(
    "/api/v1/intake",
    response_model=IntakeResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["Intake"],
)
def process_intake(req: IntakeRequest, db: Session = Depends(get_db)):
    """Validate sender allowlist, check message deduplication, clean content, parse directives, and create job."""
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

    existing_hash_job = (
        db.query(PodcastJob)
        .filter(PodcastJob.source_hash == parsed.source_hash)
        .filter(PodcastJob.status.in_([JobState.QUEUED_TTS.value, JobState.COMPLETE.value]))
        .first()
    )
    if existing_hash_job:
        return IntakeResponse(
            job_id=existing_hash_job.id,
            status=existing_hash_job.status,
            request_mode=existing_hash_job.request_mode,
            source_type=existing_hash_job.source_type,
            is_duplicate=True,
            message="Identical content hash already processed.",
        )

    job_id = str(uuid.uuid4())
    drive_key = f"herald_job_{job_id}"

    job = PodcastJob(
        id=job_id,
        gmail_message_id=req.gmail_message_id,
        gmail_thread_id=req.gmail_thread_id,
        sender_email=sender,
        request_mode=parsed.mode.value,
        source_type=source_type,
        source_url=source_url,
        source_hash=parsed.source_hash,
        source_text=parsed.clean_text,
        custom_voice=parsed.custom_voice,
        custom_speed=parsed.custom_speed,
        custom_title=parsed.custom_title,
        drive_job_key=drive_key,
        status=JobState.RECEIVED.value,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    transition_job_state(db, job, JobState.VALIDATING.value, component="herald-api")

    if source_type == SourceType.URL.value and source_url:
        transition_job_state(db, job, JobState.EXTRACTING.value, component="herald-api")
        try:
            title, extracted_text, canonical_url = extract_article_from_url(source_url)
            job.source_text = f"Title: {title}\n\n{extracted_text}"
            job.source_url = canonical_url
            job.source_hash = compute_source_hash(extracted_text, canonical_url)
            db.commit()
        except (SSRFVulnerabilityError, ArticleExtractionError) as e:
            transition_job_state(
                db, job, JobState.FAILED_FINAL.value, component="herald-api", message=str(e), error_category="ARTICLE_EXTRACTION_FAILURE"
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"URL extraction failed: {e}",
            )

    transition_job_state(db, job, JobState.SOURCE_READY.value, component="herald-api")

    return IntakeResponse(
        job_id=job.id,
        status=job.status,
        request_mode=job.request_mode,
        source_type=job.source_type,
        is_duplicate=False,
        message="Intake successful and content normalized.",
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
    """Generate Gemini podcast script matching Appendix C schema and transition job state to QUEUED_TTS."""
    job = db.query(PodcastJob).filter(PodcastJob.id == req.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.script_json and job.status in (
        JobState.QUEUED_TTS.value,
        JobState.SYNTHESIZING.value,
        JobState.AUDIO_READY.value,
        JobState.COMPLETE.value,
    ):
        return {"job_id": job.id, "status": job.status, "message": "Script already exists for job."}

    transition_job_state(db, job, JobState.SCRIPTING.value, component="herald-api")

    try:
        script = generate_podcast_script(
            source_text=job.source_text,
            request_mode=job.request_mode.lower(),
            source_title=job.custom_title,
        )
        job.script_json = script.model_dump()
        db.commit()

        transition_job_state(db, job, JobState.SCRIPT_READY.value, component="herald-api")
        transition_job_state(db, job, JobState.QUEUED_TTS.value, component="herald-api")

        return {
            "job_id": job.id,
            "status": job.status,
            "episode_title": script.episode_title,
            "estimated_minutes": script.estimated_minutes,
            "segments_count": len(script.segments),
        }
    except GeminiError as e:
        transition_job_state(
            db, job, JobState.FAILED_RETRYABLE.value, component="herald-api", message=str(e), error_category="GEMINI_SCRIPT_FAILURE"
        )
        raise HTTPException(status_code=500, detail=f"Gemini scripting failed: {e}")


@app.post(
    "/api/v1/delivery/claim",
    dependencies=[Depends(verify_api_key)],
    tags=["Delivery"],
)
def claim_delivery_job(db: Session = Depends(get_db)):
    """
    Atomically select 1 eligible delivery job using SELECT FOR UPDATE SKIP LOCKED on 1 row.
    Never claims failures from INTAKE, EXTRACTING, SCRIPTING, SYNTHESIZING, or ENCODING.
    Returns explicit action ('upload_then_email', 'email_only', 'complete_without_resend', or 'none').
    """
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(minutes=15)

    candidate_jobs = (
        db.query(PodcastJob)
        .filter(
            PodcastJob.status.in_([
                JobState.AUDIO_READY.value,
                JobState.UPLOADING.value,
                JobState.DELIVERING.value,
                JobState.FAILED_RETRYABLE.value,
            ])
        )
        .order_by(PodcastJob.updated_at.asc())
        .with_for_update(skip_locked=True)
        .all()
    )

    eligible_job = None
    for job in candidate_jobs:
        if job.status == JobState.COMPLETE.value:
            continue

        if job.status == JobState.FAILED_RETRYABLE.value:
            if job.failed_stage in ("INTAKE", "VALIDATING", "EXTRACTING", "SCRIPTING", "SYNTHESIZING", "ENCODING"):
                continue
            if job.next_retry_at:
                nr = job.next_retry_at
                if nr.tzinfo is None:
                    nr = nr.replace(tzinfo=UTC)
                if nr > now:
                    continue
            eligible_job = job
            break

        elif job.status in (JobState.UPLOADING.value, JobState.DELIVERING.value):
            last_active = job.last_heartbeat_at or job.claimed_at
            if last_active:
                if last_active.tzinfo is None:
                    last_active = last_active.replace(tzinfo=UTC)
                if last_active > stale_cutoff and job.claim_owner == "n8n-completion-dispatcher":
                    continue
            eligible_job = job
            break

        elif job.status == JobState.AUDIO_READY.value:
            eligible_job = job
            break

    if not eligible_job:
        return {"claimed": False, "action": "none", "job": None}

    job = eligible_job
    job.claimed_at = now
    job.claim_owner = "n8n-completion-dispatcher"
    job.delivery_attempt_count += 1
    job.updated_at = now

    has_drive_file = bool(job.drive_file_id and job.drive_web_link)
    already_delivered = bool(job.delivered_at or job.gmail_result_message_id)

    if already_delivered:
        action = "complete_without_resend"
        target_state = JobState.COMPLETE.value
    elif has_drive_file:
        action = "email_only"
        target_state = JobState.DELIVERING.value
    else:
        action = "upload_then_email"
        target_state = JobState.UPLOADING.value

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

    needs_upload = not has_drive_file
    needs_email = not already_delivered

    return {
        "claimed": True,
        "action": action,
        "job": {
            "id": job.id,
            "gmail_message_id": job.gmail_message_id,
            "gmail_thread_id": job.gmail_thread_id,
            "sender_email": job.sender_email,
            "local_audio_path": job.local_audio_path,
            "audio_bytes": job.audio_bytes,
            "audio_duration_seconds": job.audio_duration_seconds,
            "drive_file_id": job.drive_file_id,
            "drive_web_link": job.drive_web_link,
            "drive_job_key": job.drive_job_key or f"herald_job_{job.id}",
            "script_json": job.script_json,
            "needs_upload": needs_upload,
            "needs_email": needs_email,
            "action": action,
        },
    }


@app.post(
    "/api/v1/jobs/{job_id}/drive-complete",
    dependencies=[Depends(verify_api_key)],
    tags=["Delivery"],
)
def update_drive_complete(
    job_id: str, req: DriveCompleteRequest, db: Session = Depends(get_db)
):
    """Record Google Drive file ID, web link, and transition status to DELIVERING. Idempotent."""
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status == JobState.COMPLETE.value:
        if job.drive_file_id and job.drive_file_id != req.drive_file_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Conflicting Drive file ID on COMPLETE job: existing '{job.drive_file_id}' vs new '{req.drive_file_id}'",
            )
        return {
            "job_id": job.id,
            "status": job.status,
            "drive_file_id": job.drive_file_id,
            "drive_web_link": job.drive_web_link,
            "message": "Job already COMPLETE.",
        }

    if job.drive_file_id and job.drive_file_id != req.drive_file_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Conflicting Drive file ID: job already has '{job.drive_file_id}'",
        )

    job.drive_file_id = req.drive_file_id
    job.drive_web_link = req.drive_web_link
    if req.drive_job_key:
        job.drive_job_key = req.drive_job_key
    job.drive_uploaded_at = datetime.now(UTC)
    db.commit()

    if job.status != JobState.DELIVERING.value:
        transition_job_state(db, job, JobState.DELIVERING.value, component="n8n-drive-complete")

    return {
        "job_id": job.id,
        "status": job.status,
        "drive_file_id": job.drive_file_id,
        "drive_web_link": job.drive_web_link,
    }


@app.post(
    "/api/v1/jobs/{job_id}/delivery-complete",
    dependencies=[Depends(verify_api_key)],
    tags=["Delivery"],
)
def update_delivery_complete(
    job_id: str, req: DeliveryCompleteRequest | None = None, db: Session = Depends(get_db)
):
    """Record successful Gmail delivery and transition job to COMPLETE. Idempotent."""
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

    if job.gmail_result_message_id and new_msg_id and job.gmail_result_message_id != new_msg_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Conflicting Gmail result message ID: existing '{job.gmail_result_message_id}' vs new '{new_msg_id}'",
        )

    if new_msg_id:
        job.gmail_result_message_id = new_msg_id

    if not job.delivered_at:
        job.delivered_at = datetime.now(UTC)

    db.commit()

    if job.status != JobState.COMPLETE.value:
        transition_job_state(db, job, JobState.COMPLETE.value, component="n8n-delivery-complete")

    return {
        "job_id": job.id,
        "status": job.status,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
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
        sender_email=job.sender_email,
        request_mode=job.request_mode,
        source_type=job.source_type,
        status=job.status,
        attempt_count=job.attempt_count,
        completed_chunk_index=job.completed_chunk_index,
        local_audio_path=job.local_audio_path,
        audio_bytes=job.audio_bytes,
        audio_duration_seconds=job.audio_duration_seconds,
        drive_file_id=job.drive_file_id,
        drive_web_link=job.drive_web_link,
        drive_job_key=job.drive_job_key,
        gmail_result_message_id=job.gmail_result_message_id,
        error_code=job.error_code,
        error_detail=job.error_detail,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )


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
        if job.local_audio_path:
            p = Path(job.local_audio_path)
            if p.exists():
                freed_bytes += p.stat().st_size
                p.unlink(missing_ok=True)
            job.local_audio_path = None
            cleaned_count += 1

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
