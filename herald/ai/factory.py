"""
AI Provider Factory for Herald.
Instantiates and caches configured AIProvider instances with full provider coverage,
capability-aware dispatch, and dedicated research provider resolution.
"""

from herald.ai.anthropic_provider import AnthropicProvider
from herald.ai.base import AIProvider
from herald.ai.cloudflare_provider import CloudflareProvider
from herald.ai.gemini_provider import GeminiProvider
from herald.ai.groq_provider import GroqProvider
from herald.ai.literal_provider import LiteralProvider
from herald.ai.mistral_provider import MistralProvider
from herald.ai.ollama_provider import OllamaProvider
from herald.ai.openai_provider import OpenAIProvider
from herald.ai.openrouter_provider import OpenRouterProvider
from herald.config import settings

_provider_instance: AIProvider | None = None
_last_provider_config: tuple | None = None

_research_provider_instance: AIProvider | None = None
_last_research_config: tuple | None = None


def create_ai_provider(provider_name: str | None = None) -> AIProvider | None:
    """Create a new AIProvider instance by name."""
    prov_name = (provider_name or settings.AI_PROVIDER or "").lower().strip()
    if prov_name in ("none", "literal", ""):
        return LiteralProvider()
    if prov_name == "gemini":
        return GeminiProvider()
    if prov_name == "groq":
        return GroqProvider()
    if prov_name == "openrouter":
        return OpenRouterProvider()
    if prov_name == "mistral":
        return MistralProvider()
    if prov_name in ("cloudflare", "cloudflare_workers_ai"):
        return CloudflareProvider()
    if prov_name == "anthropic":
        return AnthropicProvider()
    if prov_name == "openai":
        return OpenAIProvider()
    if prov_name == "ollama":
        return OllamaProvider()
    return None


def get_ai_provider() -> AIProvider | None:
    """
    Return configured primary AIProvider instance, or None if no AI provider is configured.
    Maintains a persistent provider instance so health caching is preserved across calls.
    """
    global _provider_instance, _last_provider_config
    prov_name = (settings.AI_PROVIDER or "").lower().strip()
    if prov_name in ("none", "literal", ""):
        return None

    current_config = (
        prov_name,
        getattr(settings, "GEMINI_API_KEY", ""),
        getattr(settings, "GEMINI_MODEL", ""),
        getattr(settings, "GROQ_API_KEY", ""),
        getattr(settings, "GROQ_MODEL", ""),
        getattr(settings, "OPENROUTER_API_KEY", ""),
        getattr(settings, "OPENROUTER_MODEL", ""),
        getattr(settings, "MISTRAL_API_KEY", ""),
        getattr(settings, "MISTRAL_MODEL", ""),
        getattr(settings, "CLOUDFLARE_API_TOKEN", ""),
        getattr(settings, "CLOUDFLARE_ACCOUNT_ID", ""),
        getattr(settings, "CLOUDFLARE_AI_MODEL", ""),
        getattr(settings, "CLOUDFLARE_MODEL", ""),
        getattr(settings, "ANTHROPIC_API_KEY", ""),
        getattr(settings, "ANTHROPIC_MODEL", ""),
        getattr(settings, "OPENAI_API_KEY", ""),
        getattr(settings, "OPENAI_MODEL", ""),
        getattr(settings, "OPENAI_API_BASE", ""),
        getattr(settings, "OLLAMA_BASE_URL", ""),
        getattr(settings, "OLLAMA_MODEL", ""),
    )

    if _provider_instance is not None and _last_provider_config == current_config:
        return _provider_instance

    _provider_instance = create_ai_provider(prov_name)
    _last_provider_config = current_config
    return _provider_instance


def get_research_provider() -> AIProvider | None:
    """
    Return configured research provider capable of Google Search Grounding.
    Defaults to RESEARCH_PROVIDER setting (default 'gemini').
    """
    global _research_provider_instance, _last_research_config
    r_prov = (getattr(settings, "RESEARCH_PROVIDER", "gemini") or "gemini").lower().strip()

    current_config = (
        r_prov,
        getattr(settings, "GEMINI_API_KEY", ""),
        getattr(settings, "GEMINI_RESEARCH_MODEL", ""),
    )

    if _research_provider_instance is not None and _last_research_config == current_config:
        return _research_provider_instance

    prov = create_ai_provider(r_prov)
    if prov and prov.capabilities.research_grounding:
        _research_provider_instance = prov
    else:
        _research_provider_instance = None

    _last_research_config = current_config
    return _research_provider_instance


def reset_ai_provider() -> None:
    """Reset cached provider singletons (used for testing or dynamic config changes)."""
    global _provider_instance, _last_provider_config, _research_provider_instance, _last_research_config
    _provider_instance = None
    _last_provider_config = None
    _research_provider_instance = None
    _last_research_config = None
