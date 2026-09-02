from abc import ABC, abstractmethod
from typing import Any
from herald.gemini.schema import PodcastScriptResponse


class AIProvider(ABC):
    """Abstract base class for external AI script generation and health monitoring."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the AI provider, e.g. 'Gemini'."""
        pass

    @property
    @abstractmethod
    def configured_model(self) -> str:
        """Configured primary model identifier."""
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        """Returns True if the provider has all required credentials configured."""
        pass

    @abstractmethod
    def generate_script(
        self,
        source_text: str,
        request_mode: str = "standard",
        research_dossier: dict[str, Any] | None = None,
        source_title: str | None = None,
    ) -> PodcastScriptResponse:
        """Generate podcast script JSON matching schema."""
        pass

    @abstractmethod
    def check_connection(self, timeout_seconds: float = 5.0) -> dict[str, Any]:
        """
        Perform a non-generative or lightweight connection check.
        Returns a sanitized dictionary:
        {
            "provider": str,
            "configured": bool,
            "connected": bool,
            "model": str,
            "error": str | None
        }
        Secrets must NEVER be included in output or error strings.
        """
        pass
