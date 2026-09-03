"""
Herald AI Provider Module.
Exports provider classes, factory functions, capabilities, and canonical schemas.
"""

from herald.ai.anthropic_provider import AnthropicProvider
from herald.ai.base import AIProvider, ProviderCapabilities
from herald.ai.cloudflare_provider import CloudflareProvider
from herald.ai.factory import (
    create_ai_provider,
    get_ai_provider,
    get_research_provider,
    reset_ai_provider,
)
from herald.ai.gemini_provider import GeminiProvider
from herald.ai.groq_provider import GroqProvider
from herald.ai.literal_provider import LiteralProvider
from herald.ai.mistral_provider import MistralProvider
from herald.ai.ollama_provider import OllamaProvider
from herald.ai.openai_provider import OpenAIProvider
from herald.ai.openrouter_provider import OpenRouterProvider
from herald.ai.schema import PodcastScriptResponse, PodcastSegment
from herald.services.ai_recorder import record_ai_interaction

__all__ = [
    "AIProvider",
    "ProviderCapabilities",
    "PodcastSegment",
    "PodcastScriptResponse",
    "GeminiProvider",
    "GroqProvider",
    "OpenRouterProvider",
    "MistralProvider",
    "CloudflareProvider",
    "LiteralProvider",
    "AnthropicProvider",
    "OpenAIProvider",
    "OllamaProvider",
    "create_ai_provider",
    "get_ai_provider",
    "get_research_provider",
    "reset_ai_provider",
    "record_ai_interaction",
]
