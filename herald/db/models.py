import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from herald.db.connection import Base


class JobState(str, enum.Enum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    EXTRACTING = "EXTRACTING"
    SOURCE_READY = "SOURCE_READY"
    SCRIPTING = "SCRIPTING"
    SCRIPT_READY = "SCRIPT_READY"
    QUEUED_TTS = "QUEUED_TTS"
    SYNTHESIZING = "SYNTHESIZING"
    ENCODING = "ENCODING"
    AUDIO_READY = "AUDIO_READY"
    UPLOADING = "UPLOADING"
    DELIVERING = "DELIVERING"
    COMPLETE = "COMPLETE"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    CANCELLED = "CANCELLED"


class RequestMode(str, enum.Enum):
    BRIEF = "brief"
    STANDARD = "standard"
    DETAILED = "detailed"


class SourceType(str, enum.Enum):
    EMAIL_BODY = "email_body"
    URL = "url"


class PodcastJob(Base):
    __tablename__ = "podcast_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    gmail_message_id = Column(String(255), nullable=False, unique=True, index=True)
    gmail_thread_id = Column(String(255), nullable=True)
    sender_email = Column(String(255), nullable=False, index=True)

    request_mode = Column(String(20), nullable=False, default=RequestMode.STANDARD.value)
    source_type = Column(String(20), nullable=False, default=SourceType.EMAIL_BODY.value)
    source_url = Column(Text, nullable=True)
    source_hash = Column(String(64), nullable=False, index=True)
    source_text = Column(Text, nullable=False)

    # Optional top-of-body directives
    custom_voice = Column(String(50), nullable=True)
    custom_speed = Column(Float, nullable=True)
    custom_title = Column(String(255), nullable=True)

    # State tracking
    status = Column(String(50), nullable=False, default=JobState.RECEIVED.value, index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    synthesis_attempt_count = Column(Integer, nullable=False, default=0)
    delivery_attempt_count = Column(Integer, nullable=False, default=0)
    failed_stage = Column(String(50), nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)

    claimed_at = Column(DateTime(timezone=True), nullable=True)
    claim_owner = Column(String(100), nullable=True)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=True)

    # Gemini script output JSON (Appendix C schema)
    script_json = Column(JSON, nullable=True)

    # Audio synthesis tracking
    completed_chunk_index = Column(Integer, nullable=False, default=0)
    local_audio_path = Column(String(512), nullable=True)
    audio_bytes = Column(BigInteger, nullable=True)
    audio_sha256 = Column(String(64), nullable=True)
    audio_duration_seconds = Column(Integer, nullable=True)

    # Delivery & Google Drive metadata
    drive_file_id = Column(String(255), nullable=True)
    drive_web_link = Column(String(512), nullable=True)
    drive_job_key = Column(String(100), nullable=True, index=True)
    gmail_result_message_id = Column(String(255), nullable=True)
    drive_uploaded_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)

    # Error details
    error_code = Column(String(100), nullable=True)
    error_detail = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)

    transitions = relationship("JobStateTransition", back_populates="job", cascade="all, delete-orphan")


class JobStateTransition(Base):
    __tablename__ = "job_state_transitions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id = Column(String(36), ForeignKey("podcast_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    from_state = Column(String(50), nullable=True)
    to_state = Column(String(50), nullable=False)
    component = Column(String(50), nullable=False)
    message = Column(Text, nullable=True)
    error_category = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    job = relationship("PodcastJob", back_populates="transitions")


Index("idx_podcast_jobs_status_created", PodcastJob.status, PodcastJob.created_at)
Index("idx_podcast_jobs_claim", PodcastJob.status, PodcastJob.claimed_at)
