from herald.ai.base import AIProvider
from herald.ai.gemini_provider import GeminiProvider
from herald.config import settings

_provider_instance: AIProvider | None = None
_last_provider_config: tuple[str, str, str] | None = None


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
        settings.GEMINI_API_KEY or "",
        settings.GEMINI_MODEL or "",
    )

    if _provider_instance is not None and _last_provider_config == current_config:
        return _provider_instance

    if prov_name == "gemini":
        _provider_instance = GeminiProvider()
        _last_provider_config = current_config
        return _provider_instance

    return None


def reset_ai_provider() -> None:
    """Reset cached provider singleton (used for testing or dynamic config changes)."""
    global _provider_instance, _last_provider_config
    _provider_instance = None
    _last_provider_config = None
