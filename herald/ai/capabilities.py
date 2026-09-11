"""
Capability matrix and model capability declarations for Herald AI providers.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderCapabilities:
    """Explicit capability matrix for an AI provider."""

    script_brief: bool = True
    script_standard: bool = True
    structured_output: bool = True
    research_grounding: bool = False
    url_context_extraction: bool = False
    verification: bool = False
    usage_metrics: bool = True


@dataclass
class AIModelCapabilities:
    """Declared capabilities, limits, and defaults for a specific AI model."""

    provider_id: str
    model_id: str
    display_name: str
    selectable: bool = True
    context_window: int | None = None
    max_output: int | None = None
    structured_output: bool = True
    reasoning_support: bool = False
    tool_support: bool = False
    server_side_tool_support: bool = False
    known_request_body_limit: int | None = None
    compatibility_notes: str | None = None
    model_specific_defaults: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "selectable": self.selectable,
            "context_window": self.context_window,
            "max_output": self.max_output,
            "structured_output": self.structured_output,
            "reasoning_support": self.reasoning_support,
            "tool_support": self.tool_support,
            "server_side_tool_support": self.server_side_tool_support,
            "known_request_body_limit": self.known_request_body_limit,
            "compatibility_notes": self.compatibility_notes,
            "model_specific_defaults": dict(self.model_specific_defaults),
        }
