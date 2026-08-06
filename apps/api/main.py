import os
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.api.auth import verify_api_key
from packages.herald.config import settings
from packages.herald.db.connection import Base, engine, get_db
from packages.herald.db.models import (
    JobState,
    PodcastJob,
    SourceType,
)
from packages.herald.db.state_machine import transition_job_state
from packages.herald.extraction.email_parser import (
    compute_source_hash,
    process_email_message,
)
from packages.herald.extraction.url_extractor import (
    ArticleExtractionError,
    SSRFVulnerabilityError,
    extract_article_from_url,
)
from packages.herald.gemini.client import GeminiError, generate_podcast_script
from packages.herald.tts.kokoro_client import KokoroClient

# Initialize tables if running standalone
try:
    Base.metadata.create_all(bind=engine)
except Exception:
    pass

app = FastAPI(
    title="Herald Email-to-Podcast API",
    description="API service for Herald podcast generation intake, extraction, scripting, and job tracking.",
    version="0.1.0",
)


# Pydantic Schemas for API Requests & Responses
class IntakeRequest(BaseModel):
    gmail_message_id: str = Field(..., description="Unique Gmail Message ID for deduplication")
    gmail_thread_id: str | None = Field(None, description="Gmail Thread ID")
    sender_email: str = Field(..., description="Sender email address")
    subject: str = Field(..., description="Email subject line")
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
    job_id: str = Field(..., description="Job ID to generate Gemini script for")


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
    error_code: str | None
    error_detail: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


class DriveCompleteRequest(BaseModel):
    drive_file_id: str = Field(..., description="Google Drive File ID")
    drive_web_link: str = Field(..., description="Private Google Drive Web Link")


class DeliveryFailedRequest(BaseModel):
    error_code: str = Field(default="GMAIL_DELIVERY_FAILURE")
    error_detail: str = Field(default="Failed to deliver completion email")


@app.get("/health", tags=["Health"])
@app.get("/live", tags=["Health"])
def health_check():
    """
    Process liveness check endpoint. Returns HTTP 200 when API process is running.
    """
    return {
        "status": "live",
        "timestamp": datetime.now(UTC).isoformat(),
        "environment": settings.HERALD_ENV,
    }


@app.get("/readiness", tags=["Health"])
@app.get("/ready", tags=["Health"])
def readiness_check(db: Session = Depends(get_db)):
    """
    Service readiness check endpoint. Validates database connectivity and configuration.
    Returns HTTP 503 if database or configuration is unavailable.
    """
    db_healthy = False
    try:
        db.execute(text("SELECT 1"))
        db_healthy = True
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database readiness check failed: {e}",
        )

    kokoro_client = KokoroClient()
    kokoro_status = kokoro_client.health_check()

    config_valid = settings.is_production_valid()
    if not config_valid and settings.HERALD_ENV.lower() == "production":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Production configuration is missing required HERALD_API_KEY or EMAIL_ALLOWED_SENDERS",
        )

    return {
        "ready": True,
        "database": db_healthy,
        "kokoro_tts": kokoro_status,
        "environment": settings.HERALD_ENV,
    }


@app.post(
    "/api/v1/intake",
    response_model=IntakeResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["Intake"],
)
def process_intake(req: IntakeRequest, db: Session = Depends(get_db)):
    """
    Validate sender allowlist (fail closed!), check message deduplication, clean content, parse directives, and create job.
    """
    sender = req.sender_email.strip().lower()
    allowed_senders = settings.get_allowed_senders_list()

    # FAIL CLOSED: Reject unauthorized senders before URL extraction, Gemini, or DB job creation
    if settings.HERALD_ENV.lower() == "production" and not allowed_senders:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Security Violation: Sender allowlist is empty in production environment.",
        )

    if allowed_senders and sender not in allowed_senders:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sender address is not authorized on the system allowlist.",
        )

    # Idempotency check 1: Duplicate Gmail Message ID
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

    # Process email content and directives
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

    if parsed.is_url_dominant and parsed.detected_url:
        source_type = SourceType.URL.value
        source_url = parsed.detected_url

    # Idempotency check 2: Duplicate source content hash
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

    # Create new PodcastJob
    job = PodcastJob(
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
        status=JobState.RECEIVED.value,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Transition to VALIDATING -> SOURCE_READY
    transition_job_state(db, job, JobState.VALIDATING.value, component="herald-api")

    if source_type == SourceType.URL.value and source_url:
        transition_job_state(db, job, JobState.EXTRACTING.value, component="herald-api")
        try:
            title, extracted_text, canonical_url = extract_article_from_url(source_url)
            job.source_text = f"Title: {title}\n\n{extracted_text}"
            job.source_url = canonical_url
            # Recalculate source hash from extracted article text
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
    """
    Safely extract public article text with SSRF protection.
    """
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
    """
    Generate Gemini podcast script matching Appendix C schema and transition job state to QUEUED_TTS.
    """
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
            source_url=job.source_url,
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
    Atomically select 1 AUDIO_READY job using SELECT ... FOR UPDATE SKIP LOCKED,
    transition state to UPLOADING, and return job metadata.
    """
    job = (
        db.query(PodcastJob)
        .filter(PodcastJob.status == JobState.AUDIO_READY.value)
        .order_by(PodcastJob.updated_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )

    if not job:
        return {"claimed": False, "job": None}

    job.claimed_at = datetime.now(UTC)
    job.claim_owner = "n8n-completion-dispatcher"
    job.delivery_attempt_count += 1
    db.commit()

    transition_job_state(db, job, JobState.UPLOADING.value, component="n8n")

    return {
        "claimed": True,
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
            "script_json": job.script_json,
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
    """
    Record Google Drive file ID, link, and transition status to DELIVERING. Idempotent.
    """
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.drive_file_id = req.drive_file_id
    job.drive_web_link = req.drive_web_link
    job.drive_uploaded_at = datetime.now(UTC)
    db.commit()

    if job.status != JobState.DELIVERING.value and job.status != JobState.COMPLETE.value:
        transition_job_state(db, job, JobState.DELIVERING.value, component="n8n")

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
def update_delivery_complete(job_id: str, db: Session = Depends(get_db)):
    """
    Record successful Gmail delivery and transition job to COMPLETE. Idempotent.
    """
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.delivered_at = datetime.now(UTC)
    db.commit()

    if job.status != JobState.COMPLETE.value:
        transition_job_state(db, job, JobState.COMPLETE.value, component="n8n")

    return {"job_id": job.id, "status": job.status, "completed_at": job.completed_at.isoformat()}


@app.post(
    "/api/v1/jobs/{job_id}/delivery-failed",
    dependencies=[Depends(verify_api_key)],
    tags=["Delivery"],
)
def update_delivery_failed(
    job_id: str, req: DeliveryFailedRequest, db: Session = Depends(get_db)
):
    """
    Record delivery failure without deleting existing Drive metadata.
    """
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    transition_job_state(
        db,
        job,
        JobState.FAILED_RETRYABLE.value,
        component="n8n",
        message=req.error_detail,
        error_category=req.error_code,
    )
    return {"job_id": job.id, "status": job.status}


@app.get(
    "/api/v1/jobs/{job_id}",
    response_model=JobStatusResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["Jobs"],
)
def get_job_status(job_id: str, db: Session = Depends(get_db)):
    """
    Get detailed status for a podcast job.
    """
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
    Administrator retry endpoint to resume a failed job from its appropriate stage.
    """
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status not in (JobState.FAILED_RETRYABLE.value, JobState.FAILED_FINAL.value):
        raise HTTPException(status_code=400, detail=f"Job is in state '{job.status}', not FAILED")

    target_state = JobState.QUEUED_TTS.value
    if not job.script_json:
        target_state = JobState.SCRIPTING.value
    elif job.drive_file_id and job.drive_web_link:
        target_state = JobState.DELIVERING.value
    elif job.local_audio_path and os.path.exists(job.local_audio_path):
        target_state = JobState.UPLOADING.value

    job.attempt_count += 1
    job.error_code = None
    job.error_detail = None
    db.commit()

    transition_job_state(
        db, job, target_state, component="herald-api", message="Job manually retried by admin", force=True
    )
    return {"job_id": job.id, "status": job.status, "attempt_count": job.attempt_count}
