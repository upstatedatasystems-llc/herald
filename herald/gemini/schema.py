"""
Gemini Research and Verification Schemas for Herald.
Re-exports canonical PodcastSegment and PodcastScriptResponse from herald.ai.schema,
and defines Gemini-specific grounded research and audit models.
"""

from pydantic import BaseModel, ConfigDict, Field

# Re-export provider-neutral script models for backward compatibility
from herald.ai.schema import PodcastScriptResponse, PodcastSegment

__all__ = [
    "PodcastSegment",
    "PodcastScriptResponse",
    "ResearchSource",
    "VerificationItem",
    "UsefulContextItem",
    "ResearchDossierResponse",
    "ResearchAuditResponse",
    "FidelityAuditResponse",
]


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
