"""
Groq Cloud AI Provider Implementation for Herald.
Reuses OpenAI-compatible schema with Groq API endpoint.
"""

from herald.ai.openai_provider import OpenAIProvider
from herald.config import settings


class GroqProvider(OpenAIProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        super().__init__(
            api_key=api_key or settings.GROQ_API_KEY,
            model=model or settings.GROQ_MODEL or "llama-3.3-70b-versatile",
            api_base="https://api.groq.com/openai/v1",
            provider_name="Groq",
        )
