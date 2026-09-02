from typing import Any
from pydantic import BaseModel, Field


class HeraldRequest(BaseModel):
    """Transport-neutral input representation for Herald podcast jobs."""
    source_text: str | None = None
    source_url: str | None = None
    request_mode: str = "literal"
    research_depth: str | None = None
    requester_identity: str = ""
    delivery_target: str = ""
    custom_voice: str | None = None
    custom_speed: float | None = None
    custom_title: str | None = None
    tts_chunk_chars: int | None = 500
    verify_final_script: bool = False
    transport: str = "telegram"  # "telegram", "email", "api"
    transport_message_id: str | None = None
    transport_metadata: dict[str, Any] = Field(default_factory=dict)


class HeraldResponse(BaseModel):
    """Transport-neutral response returned after core intake & script queueing."""
    job_id: str
    status: str
    request_mode: str
    source_type: str
    is_duplicate: bool
    message: str
    episode_title: str | None = None
    estimated_minutes: int | None = None
    error_category: str | None = None
