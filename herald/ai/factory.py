"""
AI Provider Factory for Herald.
Provides backward-compatible helper functions delegating to authoritative herald.ai.registry.
Note: Pipeline job execution must NOT use get_ai_provider() directly;
instead use execute_with_failover() deriving from the job's snapshotted provider chain.
"""


from herald.ai.base import AIProvider
from herald.ai.registry import create_provider
from herald.config import settings

_global_provider: AIProvider | None = None


def create_ai_provider(provider_name: str | None = None, model: str | None = None) -> AIProvider | None:
    """Create a new AIProvider instance by name."""
    prov_name = (provider_name or settings.AI_PROVIDER or "literal").lower().strip()
    if prov_name in ("none", "literal", ""):
        from herald.ai.literal_provider import LiteralProvider
        return LiteralProvider()
    return create_provider(prov_name, model_id=model)


def get_ai_provider(provider_name: str | None = None, model: str | None = None) -> AIProvider | None:
    """
    Return an AIProvider instance for diagnostics, status checks, or legacy callers.
    Job execution must NOT use this global helper; use the job's snapshotted provider chain instead.
    """
    global _global_provider
    prov_name = (provider_name or settings.AI_PROVIDER or "").lower().strip()
    if prov_name in ("none", "literal", ""):
        return None
    if provider_name is None and model is None:
        if _global_provider is not None and getattr(_global_provider, "provider_name", "").lower() == prov_name:
            return _global_provider
        _global_provider = create_provider(prov_name, model_id=model)
        return _global_provider
    return create_provider(prov_name, model_id=model)


def reset_ai_provider() -> None:
    """Reset the cached global AI provider instance."""
    global _global_provider
    _global_provider = None

