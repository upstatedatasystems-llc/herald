from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class BaseTTSEngine(ABC):
    """
    Abstract interface for Herald Text-to-Speech engines.
    Allows swapping Kokoro for another engine without modifying worker workflows.
    """

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """
        Check engine health, model availability, and service readiness.
        """

    @abstractmethod
    def synthesize_chunk(
        self,
        text: str,
        output_path: Path,
        voice: str = "af_heart",
        speed: float = 1.0,
    ) -> Path:
        """
        Synthesize text into an audio file at output_path.
        """
