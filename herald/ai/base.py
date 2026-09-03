"""
Abstract Base Class and Capabilities Contract for Herald AI Providers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from herald.ai.schema import PodcastScriptResponse


def load_system_prompt() -> str:
    """Load canonical system prompt from prompts directory or fallback string."""
    prompt_file = Path(__file__).parent.parent.parent / "prompts" / "podcast_script" / "prompt.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return "Transform the provided source content into a podcast script JSON matching schema."


@dataclass(frozen=True)
class ProviderCapabilities:
    """Explicit capability matrix for an AI provider."""

    script_brief: bool = True
    script_standard: bool = True
    structured_output: bool = True
    research_grounding: bool = False
    usage_metrics: bool = True


class AIProvider(ABC):
    """Abstract base class for AI script generation, capability declaration, and health monitoring."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name (e.g. 'Gemini', 'Groq', 'OpenRouter', 'None (Literal)')."""

    @property
    @abstractmethod
    def configured_model(self) -> str:
        """The configured model identifier."""

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Declared provider capabilities. Defaults to standard scripting capabilities."""
        return ProviderCapabilities()

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if credentials and configuration are present."""

    @abstractmethod
    def generate_script(
        self,
        source_text: str,
        request_mode: str = "standard",
        research_dossier: dict[str, Any] | None = None,
        source_title: str | None = None,
        job_id: str | None = None,
    ) -> PodcastScriptResponse:
        """Generate structured podcast script from source text."""

    @abstractmethod
    def check_connection(self, timeout_seconds: float = 5.0, force_refresh: bool = False) -> dict[str, Any]:
        """
        Check connectivity with the AI provider.
        Returns a dict: {"provider": str, "configured": bool, "connected": bool, "model": str, "error": str | None}
        """
