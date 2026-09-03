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


class ResearchSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(..., description="Canonical source ID, e.g. S1, S2")
    title: str = Field(..., description="Title of the grounded research source")
    url: str = Field(..., description="Canonical URL of the source")
    domain: str = Field(..., description="Domain name of the source")
    retrieved_at: str = Field(..., description="ISO timestamp when retrieved")
    search_query: str = Field(..., description="Search query that surfaced this source")


class VerificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_claim: str = Field(..., description="Fact or claim from the primary source")
    status: str = Field(..., description="Status: supported | contradicted | qualified | outdated | uncertain")
    notes: str = Field(..., description="Detailed verification notes and context")
    source_ids: list[str] = Field(default_factory=list, description="List of source_ids from research_sources registry")


class UsefulContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: str = Field(..., description="Additional verified fact or background context")
    why_it_matters: str = Field(..., description="Relevance and importance to the listener")
    source_ids: list[str] = Field(default_factory=list, description="List of source_ids from research_sources registry")


class ResearchDossierResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_summary: str = Field(..., description="Summary overview of original source")
    verification: list[VerificationItem] = Field(default_factory=list, description="Claim verification entries")
    useful_context: list[UsefulContextItem] = Field(default_factory=list, description="Enriched context entries")
    outdated_or_uncertain: list[str] = Field(default_factory=list, description="Outdated claims or unresolved uncertainties")
    research_sources: list[ResearchSource] = Field(default_factory=list, description="Canonical research sources registry")
    material_sources: list[str] = Field(default_factory=list, description="List of source_ids materially used to support research")


class ResearchAuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unsupported_claims: list[str] = Field(default_factory=list)
    misrepresented_source_claims: list[str] = Field(default_factory=list)
    research_claims_without_evidence: list[str] = Field(default_factory=list)
    contradictions_not_disclosed: list[str] = Field(default_factory=list)
    important_verified_information_omitted: list[str] = Field(default_factory=list)
    changed_numbers_or_units: list[str] = Field(default_factory=list)
    citation_mapping_failures: list[str] = Field(default_factory=list)
    has_material_issues: bool = Field(..., description="True if material defects were found that require repair")
    repair_instructions: str | None = Field(default=None, description="Specific instructions for script repair if material issues exist")


class FidelityAuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unsupported_factual_claims: list[str] = Field(default_factory=list)
    incorrect_numbers_dates_names: list[str] = Field(default_factory=list)
    incorrect_entity_relationships: list[str] = Field(default_factory=list)
    material_source_misrepresentation: list[str] = Field(default_factory=list)
    important_omissions_material_meaning: list[str] = Field(default_factory=list)
    excessive_certainty: list[str] = Field(default_factory=list)
    accidental_invented_context: list[str] = Field(default_factory=list)
    has_material_issues: bool = Field(..., description="True if material fidelity defects exist requiring repair")
    repair_instructions: str | None = Field(default=None, description="Specific instructions for script repair if material issues exist")

