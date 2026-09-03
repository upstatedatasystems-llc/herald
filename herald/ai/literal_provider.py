"""
Literal Deterministic AI Provider Implementation for Herald.
Generates local podcast scripts deterministically with zero external AI calls and zero AI interaction records.
"""

from typing import Any

from herald.ai.base import AIProvider
from herald.gemini.schema import PodcastScriptResponse
from herald.literal.script_generator import generate_literal_script


class LiteralProvider(AIProvider):
    @property
    def provider_name(self) -> str:
        return "None (Literal)"

    @property
    def configured_model(self) -> str:
        return "local-literal-chunker"

    def is_configured(self) -> bool:
        return True

    def check_connection(self, timeout_seconds: float = 5.0, force_refresh: bool = False) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "configured": True,
            "connected": True,
            "model": self.configured_model,
            "error": None,
        }

    def generate_script(
        self,
        source_text: str,
        request_mode: str = "standard",
        research_dossier: dict[str, Any] | None = None,
        source_title: str | None = None,
        job_id: str | None = None,
    ) -> PodcastScriptResponse:
        # Literal mode makes ZERO external AI interactions
        return generate_literal_script(
            source_text=source_text,
            source_title=source_title,
        )
