"""
Canonical Provider-Neutral Podcast Script Schema for Herald.
Defines the core Pydantic models for podcast segments and structured script responses.
All AI providers, script generators, and pipeline consumers adhere to this neutral contract.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PodcastSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int = Field(..., description="1-indexed sequence number of narration segment", ge=1)
    heading: str = Field(..., description="Section heading or topic title")
    narration: str = Field(..., description="Spoken narration text for TTS synthesis")

    @field_validator("heading", "narration")
    def validate_non_empty_strings(cls, v: str, info) -> str:
        s = v.strip()
        if not s:
            raise ValueError(f"Segment field '{info.field_name}' must not be empty.")
        return s


class PodcastScriptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_title: str = Field(..., description="Catchy descriptive title for podcast episode")
    episode_description: str = Field(..., description="Summary overview of the episode")
    estimated_minutes: int | None = Field(default=None, description="Legacy estimated spoken duration in minutes")
    source_title: str | None = Field(default=None, description="Title of source article or email")
    segments: list[PodcastSegment] = Field(..., min_length=1, description="Ordered narration segments")
    warnings: list[str] = Field(..., description="Any content warnings or extraction notes")

    @field_validator("estimated_minutes")
    def validate_estimated_minutes(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("estimated_minutes must be >= 1 if provided.")
        return v

    @field_validator("episode_title", "episode_description")
    def validate_non_empty_top_fields(cls, v: str, info) -> str:
        s = v.strip()
        if not s:
            raise ValueError(f"Field '{info.field_name}' must not be empty after trimming.")
        return s

    @field_validator("segments")
    def validate_segment_order(cls, v: list[PodcastSegment]) -> list[PodcastSegment]:
        if not v:
            raise ValueError("Script must contain at least one narration segment.")

        expected = 1
        seen_orders = set()
        for seg in v:
            if seg.order in seen_orders:
                raise ValueError(f"Duplicate segment order found: {seg.order}")
            seen_orders.add(seg.order)

            if seg.order != expected:
                raise ValueError(f"Segment order error: expected sequential order starting at 1, but got {seg.order} at position {expected}")
            expected += 1
        return v


def rebase_isolated_section_orders(data: dict[str, Any]) -> dict[str, Any]:
    """
    Deterministically rebase response-local segment orders for isolated section generation.

    If an isolated section response is otherwise valid but its segment orders are a
    contiguous sequence starting at an offset base (e.g. global section number 3 -> [3], [3, 4]),
    safely rebase them to start at 1 ([1], [1, 2]).

    Strict safety rules:
    - [1, 2, ...] is returned unchanged.
    - Contiguous [B, B+1, ...] with B > 1 is rebased to [1, 2, ...].
    - Does NOT rebase:
      * Duplicate orders (e.g. [3, 3])
      * Gaps (e.g. [3, 5])
      * Non-monotonic orders (e.g. [4, 3])
      * Zero or negative values (e.g. [0], [-1, 0])
      * Non-integer or boolean orders
      * Malformed segments (not a dict or missing order)
      * Empty segments
    Any unrebasing-eligible structure is returned unmodified so that canonical
    PodcastScriptResponse validation enforces the strict schema contract.
    """
    if not isinstance(data, dict):
        return data

    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        return data

    orders: list[int] = []
    for s in segments:
        if not isinstance(s, dict):
            return data
        o = s.get("order")
        if isinstance(o, bool) or not isinstance(o, int) or o <= 0:
            return data
        orders.append(o)

    # If already sequential starting at 1, nothing to rebase
    if orders == list(range(1, len(orders) + 1)):
        return data

    base = orders[0]
    # If base > 1 and orders is strictly contiguous [base, base+1, ... base+len-1]
    if base > 1 and orders == list(range(base, base + len(orders))):
        import logging
        logger = logging.getLogger("herald.ai.schema")
        logger.info(
            "Rebasing isolated section segment orders from %s to %s (base %s -> 1)",
            orders,
            list(range(1, len(orders) + 1)),
            base,
        )
        new_segments = []
        for idx, s in enumerate(segments, 1):
            new_seg = dict(s)
            new_seg["order"] = idx
            new_segments.append(new_seg)
        result = dict(data)
        result["segments"] = new_segments
        return result

    return data


def parse_isolated_section_response(data: dict[str, Any] | str | PodcastScriptResponse) -> PodcastScriptResponse:
    """
    Parse and validate a provider response for an isolated section.
    Applies deterministic segment order rebasing before canonical PodcastScriptResponse validation.
    """
    if isinstance(data, PodcastScriptResponse):
        return data

    if isinstance(data, str):
        import json
        parsed = json.loads(data)
    elif isinstance(data, dict):
        parsed = data
    else:
        raise ValueError(f"Expected dict, JSON string, or PodcastScriptResponse, got {type(data).__name__}")

    normalized = rebase_isolated_section_orders(parsed)
    return PodcastScriptResponse(**normalized)


def is_isolated_section_instruction(instructions: str | None) -> bool:
    """Check if generation instructions declare an isolated/standalone section generation task."""
    if not instructions:
        return False
    text = instructions.upper()
    return (
        "STANDALONE RESPONSE" in text
        or "RESPONSE-LOCAL" in text
        or "ISOLATED SECTION" in text
    )


class RepetitionReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_b: int = Field(..., description="The later section index being evaluated")
    section_a: int = Field(..., description="The earlier section index where concept first appeared")
    concept_or_passage: str = Field(..., description="The distinctive phrase or passage candidate evaluated")
    is_substantive_repetition: bool = Field(
        ...,
        description=(
            "True if section_b repeats explanations, facts, or substantive narrative from section_a. "
            "False if it is merely legitimate recurring terminology, thematic callback, or technical term."
        ),
    )
    explanation: str = Field(..., description="Reasoning for why this is or is not substantive repetition")
    passage_to_repair: str | None = Field(
        default=None,
        description="The specific repetitive passage in section_b that should be rewritten/condensed, if substantive",
    )


class RepetitionReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_substantive_repetition: bool = Field(..., description="True if any candidates contain substantive repetition")
    reviews: list[RepetitionReviewItem] = Field(
        default_factory=list,
        description="Structured repetition evaluation for each candidate",
    )


