
from pydantic import BaseModel, Field, field_validator


class PodcastSegment(BaseModel):
    order: int = Field(..., description="1-indexed sequence number of narration segment", ge=1)
    heading: str = Field(default="Section", description="Section heading or topic title")
    narration: str = Field(..., description="Spoken narration text for TTS synthesis")

    @field_validator("narration")
    def validate_narration_not_empty(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Segment narration text must not be empty.")
        return s


class PodcastScriptResponse(BaseModel):
    episode_title: str = Field(..., description="Catchy descriptive title for podcast episode")
    episode_description: str = Field(..., description="Summary overview of the episode")
    estimated_minutes: int = Field(..., description="Estimated spoken duration in minutes", ge=1)
    source_title: str | None = Field(default=None, description="Title of source article or email")
    source_url: str | None = Field(default=None, description="Canonical source URL if available")
    segments: list[PodcastSegment] = Field(..., min_length=1, description="Ordered narration segments")
    warnings: list[str] = Field(default_factory=list, description="Any content warnings or extraction notes")

    @field_validator("episode_title")
    def validate_title_not_empty(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Episode title must not be empty.")
        return s

    @field_validator("segments")
    def validate_segment_order(cls, v: list[PodcastSegment]) -> list[PodcastSegment]:
        if not v:
            raise ValueError("Script must contain at least one narration segment.")

        expected = 1
        for seg in v:
            if seg.order != expected:
                raise ValueError(f"Segment order error: expected {expected}, got {seg.order}")
            expected += 1
        return v
