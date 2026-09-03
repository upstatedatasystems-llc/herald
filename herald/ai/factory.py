from herald.ai.anthropic_provider import AnthropicProvider
from herald.ai.base import AIProvider
from herald.ai.gemini_provider import GeminiProvider
from herald.ai.groq_provider import GroqProvider
from herald.ai.literal_provider import LiteralProvider
from herald.ai.ollama_provider import OllamaProvider
from herald.ai.openai_provider import OpenAIProvider
from herald.config import settings

_provider_instance: AIProvider | None = None
_last_provider_config: tuple | None = None


def create_ai_provider(provider_name: str | None = None) -> AIProvider | None:
    """Create a new AIProvider instance by name."""
    prov_name = (provider_name or settings.AI_PROVIDER or "").lower().strip()
    if prov_name in ("none", "literal", ""):
        return LiteralProvider()
    if prov_name == "gemini":
        return GeminiProvider()
    if prov_name == "anthropic":
        return AnthropicProvider()
    if prov_name == "openai":
        return OpenAIProvider()
    if prov_name == "groq":
        return GroqProvider()
    if prov_name == "ollama":
        return OllamaProvider()
    return None


def get_ai_provider() -> AIProvider | None:
    """
    Return configured AIProvider instance, or None if no AI provider is configured.
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
        getattr(settings, "ANTHROPIC_API_KEY", ""),
        getattr(settings, "ANTHROPIC_MODEL", ""),
        getattr(settings, "OPENAI_API_KEY", ""),
        getattr(settings, "OPENAI_MODEL", ""),
        getattr(settings, "OPENAI_API_BASE", ""),
        getattr(settings, "GROQ_API_KEY", ""),
        getattr(settings, "GROQ_MODEL", ""),
        getattr(settings, "OLLAMA_BASE_URL", ""),
        getattr(settings, "OLLAMA_MODEL", ""),
    )

    if _provider_instance is not None and _last_provider_config == current_config:
        return _provider_instance

    if prov_name == "gemini":
        _provider_instance = GeminiProvider()
    elif prov_name == "anthropic":
        _provider_instance = AnthropicProvider()
    elif prov_name == "openai":
        _provider_instance = OpenAIProvider()
    elif prov_name == "groq":
        _provider_instance = GroqProvider()
    elif prov_name == "ollama":
        _provider_instance = OllamaProvider()
    else:
        _provider_instance = None

    _last_provider_config = current_config
    return _provider_instance


def reset_ai_provider() -> None:
    """Reset cached provider singleton (used for testing or dynamic config changes)."""
    global _provider_instance, _last_provider_config
    _provider_instance = None
    _last_provider_config = None
