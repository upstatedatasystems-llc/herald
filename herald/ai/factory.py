from herald.ai.base import AIProvider
from herald.ai.gemini_provider import GeminiProvider
from herald.config import settings


def get_ai_provider() -> AIProvider | None:
    """
    Return configured AIProvider instance, or None if no AI provider is configured.
    """
    prov_name = (settings.AI_PROVIDER or "").lower().strip()
    if prov_name in ("none", "literal", ""):
        return None

    if prov_name == "gemini":
        return GeminiProvider()

    return None
