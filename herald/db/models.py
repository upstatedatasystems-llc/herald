import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    LITERAL = "literal"
    BRIEF = "brief"
    STANDARD = "standard"
    RESEARCH = "research"
    DETAILED = "detailed"


class SourceType(str, enum.Enum):
    EMAIL_BODY = "email_body"
    URL = "url"
    TEXT = "text"
    TELEGRAM_MESSAGE = "telegram_message"


class PodcastJob(Base):
    __tablename__ = "podcast_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    transport = Column(String(50), nullable=False, default="email", index=True)
    gmail_message_id = Column(String(255), nullable=True, unique=True, index=True)
    gmail_thread_id = Column(String(255), nullable=True)
    sender_email = Column(String(255), nullable=True, index=True)
    telegram_chat_id = Column(BigInteger, nullable=True, index=True)
    telegram_message_id = Column(BigInteger, nullable=True, index=True)
    telegram_user_id = Column(BigInteger, nullable=True, index=True)

    request_mode = Column(String(20), nullable=False, default=RequestMode.STANDARD.value)
    research_depth = Column(String(20), nullable=True)
    source_type = Column(String(20), nullable=False, default=SourceType.EMAIL_BODY.value)
    source_url = Column(Text, nullable=True)
    source_hash = Column(String(64), nullable=False, index=True)
    source_text = Column(Text, nullable=False)

    # Optional top-of-body or subject directives
    custom_voice = Column(String(50), nullable=True)
    custom_speed = Column(Float, nullable=True)
    custom_title = Column(String(255), nullable=True)
    tts_chunk_chars = Column(Integer, nullable=True, default=500)
    verify_final_script = Column(Boolean, nullable=True, default=False)

    gmail_received_at = Column(DateTime(timezone=True), nullable=True)

    # State tracking
    status = Column(String(50), nullable=False, default=JobState.RECEIVED.value, index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    synthesis_attempt_count = Column(Integer, nullable=False, default=0)
    delivery_attempt_count = Column(Integer, nullable=False, default=0)
    failed_stage = Column(String(50), nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)

    claimed_at = Column(DateTime(timezone=True), nullable=True)
    claim_owner = Column(String(100), nullable=True)
    claimed_by = Column(String(100), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)

    # Gemini script output JSON & verification

    script_json = Column(JSON, nullable=True)
    verify_audit_json = Column(JSON, nullable=True)
    verify_repair_count = Column(Integer, nullable=False, default=0)

    # Research mode data fields
    research_grounding_json = Column(JSON, nullable=True)
    research_json = Column(JSON, nullable=True)
    research_model = Column(String(50), nullable=True)
    research_search_count = Column(Integer, nullable=True)
    research_source_count = Column(Integer, nullable=True)
    research_audit_json = Column(JSON, nullable=True)
    research_repair_count = Column(Integer, nullable=False, default=0)

    # Audio synthesis & telemetry tracking
    completed_chunk_index = Column(Integer, nullable=False, default=0)
    local_audio_path = Column(String(512), nullable=True)
    audio_bytes = Column(BigInteger, nullable=True)
    audio_sha256 = Column(String(64), nullable=True)
    audio_duration_seconds = Column(Integer, nullable=True)
    audio_ready_at = Column(DateTime(timezone=True), nullable=True)
    kokoro_voice = Column(String(50), nullable=True)
    kokoro_speed = Column(Float, nullable=True)
    gemini_model = Column(String(50), nullable=True)
    tts_resource_metrics_json = Column(JSON, nullable=True)

    # Delivery & Google Drive metadata
    drive_file_id = Column(String(255), nullable=True)
    drive_web_link = Column(String(512), nullable=True)
    details_drive_file_id = Column(String(255), nullable=True, unique=True, index=True)
    details_drive_web_link = Column(String(512), nullable=True)
    source_drive_file_id = Column(String(255), nullable=True, unique=True, index=True)
    source_drive_web_link = Column(String(512), nullable=True)
    script_drive_file_id = Column(String(255), nullable=True, unique=True, index=True)
    script_drive_web_link = Column(String(512), nullable=True)
    diagnostics_drive_file_id = Column(String(255), nullable=True, unique=True, index=True)
    diagnostics_drive_web_link = Column(String(512), nullable=True)
    research_drive_file_id = Column(String(255), nullable=True, unique=True, index=True)
    research_drive_web_link = Column(String(512), nullable=True)
    research_notes_drive_file_id = Column(String(255), nullable=True, unique=True, index=True)
    research_notes_drive_web_link = Column(String(512), nullable=True)
    drive_job_key = Column(String(100), nullable=True, index=True)
    gmail_result_message_id = Column(String(255), nullable=True)
    drive_uploaded_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    details_finalized_at = Column(DateTime(timezone=True), nullable=True)

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
    metrics = relationship("JobProcessingMetric", back_populates="job", cascade="all, delete-orphan")
    tts_chunks = relationship("PodcastTTSChunk", back_populates="job", cascade="all, delete-orphan")


class PodcastTTSChunk(Base):
    __tablename__ = "podcast_tts_chunks"
    __table_args__ = (
        UniqueConstraint("job_id", "chunk_index", name="uq_podcast_tts_chunks_job_index"),
        Index("idx_tts_chunks_job_status", "job_id", "status"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id = Column(String(36), ForeignKey("podcast_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)
    text_hash = Column(String(64), nullable=False)
    status = Column(String(50), nullable=False, default="PENDING", index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    local_path = Column(String(512), nullable=True)
    audio_duration = Column(Float, nullable=True)
    claimed_by = Column(String(100), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    error_detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    job = relationship("PodcastJob", back_populates="tts_chunks")


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


class JobProcessingMetric(Base):
    __tablename__ = "job_processing_metrics"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id = Column(String(36), ForeignKey("podcast_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    stage = Column(String(50), nullable=False)
    substage = Column(String(50), nullable=True)
    attempt = Column(Integer, nullable=True)
    sequence_index = Column(Integer, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(BigInteger, nullable=True)
    status = Column(String(50), nullable=False)

    input_chars = Column(Integer, nullable=True)
    output_bytes = Column(BigInteger, nullable=True)
    audio_duration_ms = Column(BigInteger, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    job = relationship("PodcastJob", back_populates="metrics")


class TelegramUser(Base):
    __tablename__ = "telegram_users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    telegram_user_id = Column(BigInteger, nullable=False, unique=True, index=True)
    telegram_chat_id = Column(BigInteger, nullable=False, index=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    role = Column(String(50), nullable=False, default="owner")
    is_active = Column(Boolean, nullable=False, default=True)
    confirm_before_tts = Column(Boolean, nullable=False, default=False)
    default_voice = Column(String(50), nullable=True)
    default_speed = Column(Float, nullable=True)
    default_mode = Column(String(20), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class TelegramPairingCode(Base):
    __tablename__ = "telegram_pairing_codes"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code = Column(String(50), nullable=False, unique=True, index=True)
    is_used = Column(Boolean, nullable=False, default=False, index=True)
    used_by_user_id = Column(BigInteger, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    used_at = Column(DateTime(timezone=True), nullable=True)


class TelegramPollState(Base):
    __tablename__ = "telegram_poll_state"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    last_processed_update_id = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class TelegramUpdateFailure(Base):
    __tablename__ = "telegram_update_failures"

    update_id = Column(BigInteger, primary_key=True)
    attempt_count = Column(Integer, nullable=False, default=1)
    last_error = Column(Text, nullable=True)
    is_dead_lettered = Column(Boolean, nullable=False, default=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


Index("idx_podcast_jobs_status_created", PodcastJob.status, PodcastJob.created_at)
Index("idx_podcast_jobs_claim", PodcastJob.status, PodcastJob.claimed_at)
Index("uq_podcast_jobs_telegram", PodcastJob.transport, PodcastJob.telegram_chat_id, PodcastJob.telegram_message_id, unique=True)
Index("idx_job_processing_metrics_job_stage", JobProcessingMetric.job_id, JobProcessingMetric.stage)
Index("idx_job_processing_metrics_stage_created", JobProcessingMetric.stage, JobProcessingMetric.created_at)
Index("idx_job_processing_metrics_job_seq", JobProcessingMetric.job_id, JobProcessingMetric.sequence_index)



