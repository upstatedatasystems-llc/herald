"""
OpenRouter AI Provider Implementation for Herald.
Routes script generation requests through OpenRouter's unified API endpoint.
"""

from herald.ai.openai_provider import OpenAIProvider
from herald.config import settings


class OpenRouterProvider(OpenAIProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        super().__init__(
            api_key=api_key or settings.OPENROUTER_API_KEY,
            model=model or settings.OPENROUTER_MODEL or "meta-llama/llama-3.3-70b-instruct",
            api_base="https://openrouter.ai/api/v1",
            provider_name="OpenRouter",
            custom_headers={
                "HTTP-Referer": "https://herald.local",
                "X-Title": "Herald Podcast Pipeline",
            },
        )
