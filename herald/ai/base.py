"""
Abstract Base Class and Capabilities Contract for Herald AI Providers.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from herald.ai.capabilities import ProviderCapabilities
from herald.ai.schema import PodcastScriptResponse


def load_system_prompt() -> str:
    """Load canonical system prompt from prompts directory or fallback string."""
    prompt_file = Path(__file__).parent.parent.parent / "prompts" / "podcast_script" / "prompt.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return "Transform the provided source content into a podcast script JSON matching schema."


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

    def generate_grounded_research(
        self,
        source_text: str,
        research_depth: str = "medium",
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Generate grounded web search research. Subclasses override if supported."""
        from herald.ai.errors import AIUnsupportedCapabilityError
        raise AIUnsupportedCapabilityError(f"Provider {self.provider_name} does not support Google Search Grounding")

    def normalize_research_dossier(
        self,
        source_text: str,
        grounded_research_data: dict[str, Any],
        job_id: str | None = None,
    ) -> Any:
        """Normalize research grounding data into structured dossier. Subclasses override if supported."""
        from herald.ai.errors import AIUnsupportedCapabilityError
        raise AIUnsupportedCapabilityError(f"Provider {self.provider_name} does not support research dossier normalization")

    def audit_research_script(
        self,
        source_text: str,
        research_dossier: dict[str, Any],
        script_dict: dict[str, Any],
        job_id: str | None = None,
    ) -> Any:
        """Audit research script against sources. Subclasses override if supported."""
        from herald.ai.errors import AIUnsupportedCapabilityError
        raise AIUnsupportedCapabilityError(f"Provider {self.provider_name} does not support research auditing")

    def repair_research_script(
        self,
        source_text: str,
        research_dossier: dict[str, Any],
        script_dict: dict[str, Any],
        audit_result: dict[str, Any],
        job_id: str | None = None,
    ) -> Any:
        """Repair research script based on audit findings. Subclasses override if supported."""
        from herald.ai.errors import AIUnsupportedCapabilityError
        raise AIUnsupportedCapabilityError(f"Provider {self.provider_name} does not support research script repair")

    def audit_script_fidelity(
        self,
        source_text: str,
        script_dict: dict[str, Any],
        job_id: str | None = None,
    ) -> Any:
        """Audit script fidelity against source text. Subclasses override if supported."""
        from herald.ai.errors import AIUnsupportedCapabilityError
        raise AIUnsupportedCapabilityError(f"Provider {self.provider_name} does not support fidelity verification")

    def repair_script_fidelity(
        self,
        source_text: str,
        script_dict: dict[str, Any],
        audit_result: dict[str, Any],
        job_id: str | None = None,
    ) -> Any:
        """Repair script based on fidelity audit findings. Subclasses override if supported."""
        from herald.ai.errors import AIUnsupportedCapabilityError
        raise AIUnsupportedCapabilityError(f"Provider {self.provider_name} does not support fidelity repair")

    def extract_article_via_url_context(
        self,
        url: str,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Extract article via URL context. Subclasses override if supported."""
        from herald.ai.errors import AIUnsupportedCapabilityError
        raise AIUnsupportedCapabilityError(f"Provider {self.provider_name} does not support URL context extraction")

    def distill_text(
        self,
        chunk: str,
        *,
        chunk_index: int = 0,
        total_chunks: int = 1,
        job_id: str | None = None,
    ) -> str:
        """
        Distill key narrative facts and information from a source chunk using AI.
        Preserves names, dates, numbers, attribution, qualifiers, uncertainty, and order.
        Subclasses override if supported. Default raises NotImplementedError to trigger fallback.
        """
        from herald.ai.errors import AIUnsupportedCapabilityError

        raise AIUnsupportedCapabilityError(f"Provider {self.provider_name} does not support text distillation")


# Isolated legacy compatibility delegates (DEPRECATED: use execute_with_failover directly)



