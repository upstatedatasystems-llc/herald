import os

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from packages.herald.config import settings
from packages.herald.db.connection import Base, engine, get_db
from packages.herald.db.models import (
    JobState,
    PodcastJob,
    SourceType,
)
from packages.herald.db.state_machine import transition_job_state
from packages.herald.extraction.email_parser import (
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


# Dependency security check (optional API Key header)
def verify_api_key(x_api_key: str | None = Header(None)):
    if settings.HERALD_API_KEY and settings.HERALD_API_KEY != "default-insecure-api-key":
        if x_api_key != settings.HERALD_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing X-API-Key header",
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
    audio_duration_seconds: int | None
    drive_file_id: str | None
    drive_web_link: str | None
    error_code: str | None
    error_detail: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


class DeliveryUpdateRequest(BaseModel):
    drive_file_id: str
    drive_web_link: str
    completed_delivery: bool = True


@app.get("/health", tags=["Health"])
def health_check(db: Session = Depends(get_db)):
    """
    Service liveness and health endpoint.
    """
    db_healthy = False
    try:
        db.execute("SELECT 1")
        db_healthy = True
    except Exception:
        pass

    kokoro_client = KokoroClient()
    kokoro_status = kokoro_client.health_check()

    overall = db_healthy and kokoro_status.get("healthy", False)

    return {
        "status": "healthy" if overall else "degraded",
        "database": db_healthy,
        "kokoro_tts": kokoro_status,
        "environment": settings.HERALD_ENV,
    }


@app.get("/readiness", tags=["Health"])
def readiness_check(db: Session = Depends(get_db)):
    """
    Service readiness check.
    """
    try:
        db.execute("SELECT 1")
        return {"ready": True}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database connection unavailable: {e}",
        )


@app.post("/api/v1/intake", response_model=IntakeResponse, tags=["Intake"])
def process_intake(req: IntakeRequest, db: Session = Depends(get_db)):
    """
    Validate sender allowlist, check message deduplication, clean content, and create job.
    """
    sender = req.sender_email.strip().lower()
    allowed_senders = settings.get_allowed_senders_list()

    if allowed_senders and sender not in allowed_senders:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Sender '{req.sender_email}' is not on the authorized allowlist.",
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

    # Process email content
    parsed = process_email_message(
        subject=req.subject, body_text=req.body_text, body_html=req.body_html
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
        .filter(PodcastJob.status.in_([JobState.QUEUED.value, JobState.COMPLETE.value]))
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
            db.commit()
        except (SSRFVulnerabilityError, ArticleExtractionError) as e:
            transition_job_state(
                db, job, JobState.FAILED.value, component="herald-api", message=str(e), error_category="ARTICLE_EXTRACTION_FAILURE"
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


@app.post("/api/v1/extract", response_model=ExtractUrlResponse, tags=["Extraction"])
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


@app.post("/api/v1/script/generate", tags=["Scripting"])
def generate_script_endpoint(req: GenerateScriptRequest, db: Session = Depends(get_db)):
    """
    Generate Gemini podcast script for job and transition status to QUEUED.
    """
    job = db.query(PodcastJob).filter(PodcastJob.id == req.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.script_json and job.status in (JobState.QUEUED.value, JobState.SYNTHESIZING.value, JobState.AUDIO_READY.value, JobState.COMPLETE.value):
        return {"job_id": job.id, "status": job.status, "message": "Script already exists for job."}

    transition_job_state(db, job, JobState.SCRIPTING.value, component="herald-api")

    try:
        script = generate_podcast_script(
            source_text=job.source_text,
            request_mode=job.request_mode.lower(),
            source_url=job.source_url,
        )
        job.script_json = script.model_dump()
        db.commit()

        transition_job_state(db, job, JobState.SCRIPT_READY.value, component="herald-api")
        transition_job_state(db, job, JobState.QUEUED.value, component="herald-api")

        return {
            "job_id": job.id,
            "status": job.status,
            "episode_title": script.episode_title,
            "segments_count": len(script.segments),
        }
    except GeminiError as e:
        transition_job_state(
            db, job, JobState.FAILED.value, component="herald-api", message=str(e), error_category="GEMINI_SCRIPT_FAILURE"
        )
        raise HTTPException(status_code=500, detail=f"Gemini scripting failed: {e}")


@app.get("/api/v1/jobs/{job_id}", response_model=JobStatusResponse, tags=["Jobs"])
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
        audio_duration_seconds=job.audio_duration_seconds,
        drive_file_id=job.drive_file_id,
        drive_web_link=job.drive_web_link,
        error_code=job.error_code,
        error_detail=job.error_detail,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )


@app.post("/api/v1/jobs/{job_id}/retry", tags=["Jobs"])
def retry_job(job_id: str, db: Session = Depends(get_db)):
    """
    Administrator retry endpoint to resume a failed job.
    """
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != JobState.FAILED.value:
        raise HTTPException(status_code=400, detail=f"Job is in state '{job.status}', not FAILED")

    target_state = JobState.QUEUED.value
    if not job.script_json:
        target_state = JobState.SCRIPTING.value
    elif job.local_audio_path and os.path.exists(job.local_audio_path):
        target_state = JobState.AUDIO_READY.value

    job.attempt_count += 1
    job.error_code = None
    job.error_detail = None
    db.commit()

    transition_job_state(
        db, job, target_state, component="herald-api", message="Job manually retried by admin", force=True
    )
    return {"job_id": job.id, "status": job.status, "attempt_count": job.attempt_count}


@app.post("/api/v1/jobs/{job_id}/delivery", tags=["Jobs"])
def update_delivery_metadata(
    job_id: str, req: DeliveryUpdateRequest, db: Session = Depends(get_db)
):
    """
    Update Google Drive file ID, link, and transition status to COMPLETE.
    """
    job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.drive_file_id = req.drive_file_id
    job.drive_web_link = req.drive_web_link
    db.commit()

    transition_job_state(db, job, JobState.UPLOADING.value, component="n8n", force=True)
    transition_job_state(db, job, JobState.DELIVERING.value, component="n8n", force=True)
    transition_job_state(db, job, JobState.COMPLETE.value, component="n8n", force=True)

    return {"job_id": job.id, "status": job.status, "drive_file_id": job.drive_file_id}
