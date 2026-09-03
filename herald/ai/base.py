from abc import ABC, abstractmethod
from typing import Any

from herald.gemini.schema import PodcastScriptResponse


class AIProvider(ABC):
    """Abstract base class for AI script generation and health monitoring."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name (e.g. 'Gemini', 'None (Literal)')."""

    @property
    @abstractmethod
    def configured_model(self) -> str:
        """The configured model identifier."""

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
    ) -> PodcastScriptResponse:
        """Generate structured podcast script from source text."""

    @abstractmethod
    def check_connection(
        self, timeout_seconds: float = 5.0, force_refresh: bool = False
    ) -> dict[str, Any]:
        """
        Check connectivity with the AI provider.
        Returns a dict: {"provider": str, "configured": bool, "connected": bool, "model": str, "error": str | None}
        """
