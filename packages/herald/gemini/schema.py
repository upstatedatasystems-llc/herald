
from pydantic import BaseModel, Field, field_validator


class PodcastSegment(BaseModel):
    sequence: int = Field(..., description="1-indexed sequence number of narration segment")
    speaker: str = Field(default="host", description="Speaker identifier, default 'host'")
    text: str = Field(..., description="Spoken text for this narration segment")

    @field_validator("text")
    def validate_text_not_empty(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Segment text must not be empty.")
        return s


class PodcastScriptResponse(BaseModel):
    episode_title: str = Field(..., description="Catchy descriptive title for podcast episode")
    episode_description: str = Field(..., description="Summary overview of the episode")
    source_title: str | None = Field(default=None, description="Title of source article or email")
    source_url: str | None = Field(default=None, description="Canonical source URL if available")
    requested_mode: str = Field(..., description="Requested depth: brief, standard, or detailed")
    segments: list[PodcastSegment] = Field(..., min_length=1, description="Ordered narration segments")

    @field_validator("episode_title")
    def validate_title_not_empty(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Episode title must not be empty.")
        return s

    @field_validator("segments")
    def validate_segment_sequence(cls, v: list[PodcastSegment]) -> list[PodcastSegment]:
        if not v:
            raise ValueError("Script must contain at least one narration segment.")

        expected = 1
        for seg in v:
            if seg.sequence != expected:
                raise ValueError(f"Segment sequence error: expected {expected}, got {seg.sequence}")
            expected += 1
        return v
