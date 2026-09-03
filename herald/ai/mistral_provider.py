"""
Mistral AI Provider Implementation for Herald.
Routes script generation requests through Mistral AI's Chat Completions API.
"""

from herald.ai.openai_provider import OpenAIProvider
from herald.config import settings


class MistralProvider(OpenAIProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        super().__init__(
            api_key=api_key or settings.MISTRAL_API_KEY,
            model=model or settings.MISTRAL_MODEL or "mistral-large-latest",
            api_base="https://api.mistral.ai/v1",
            provider_name="Mistral",
        )
