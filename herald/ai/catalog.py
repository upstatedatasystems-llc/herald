"""
Unified Model Discovery, Caching, and Validation Service for Herald AI Architecture.
Shared identically by /models command, /settings -> AI Models, and job resolution.
Implements restart-safe deterministic callback tokens.
"""

import hashlib
import logging
import time
from typing import Any

from herald.ai.capabilities import AIModelCapabilities
from herald.ai.registry import get_descriptor

logger = logging.getLogger("herald.ai.catalog")

_CACHE_TTL_SECONDS = 300.0
_MODEL_CACHE: dict[str, tuple[float, list[AIModelCapabilities]]] = {}


def generate_model_token(provider_id: str, model_id: str) -> str:
    """
    Generate a short, deterministic, restart-safe token for Telegram callback_data.
    Token is derived from SHA-256(provider_id + '\0' + model_id)[:10].
    Guarantees callback_data stays well within Telegram's 64-byte limit.
    """
    raw = f"{provider_id.lower().strip()}\0{model_id.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def get_models_for_provider(
    provider_id: str,
    force_refresh: bool = False,
) -> list[AIModelCapabilities]:
    """
    Retrieve available models for a provider using preference order:
    1. Cached live discovery (if within TTL)
    2. Verified catalog metadata in registry descriptor
    """
    p_id = provider_id.lower().strip()
    desc = get_descriptor(p_id)
    if not desc:
        return []

    now = time.monotonic()
    if not force_refresh and p_id in _MODEL_CACHE:
        cached_time, cached_models = _MODEL_CACHE[p_id]
        if now - cached_time < _CACHE_TTL_SECONDS:
            return cached_models

    # Default to static catalog models from registry
    models = list(desc.catalog_models)

    # Optional live discovery if provider is configured and supports discovery
    if desc.supports_model_discovery and desc.is_configured():
        try:
            # We can query live models if provider client supports it
            from herald.ai.registry import create_provider
            prov_instance = create_provider(p_id)
            if hasattr(prov_instance, "discover_models"):
                live_models = prov_instance.discover_models()
                if live_models:
                    models = live_models
        except Exception as e:
            logger.debug(f"Live model discovery failed for {p_id}, using static catalog: {e}")

    _MODEL_CACHE[p_id] = (now, models)
    return models


get_model_token = generate_model_token


def resolve_model_token(arg1: str, arg2: str | None = None) -> Any:
    """
    Statelessly resolve a model token back to its model ID or (provider_id, model_id).
    Survives bot process restarts because tokens are deterministic hashes.
    Supports:
      resolve_model_token(provider_id, token) -> model_id (or None)
      resolve_model_token(token) -> (provider_id, model_id) (or None)
    """
    from herald.ai.registry import list_registered_providers

    if arg2 is not None:
        p_id = arg1.lower().strip()
        token_clean = arg2.strip().lower()
        models = get_models_for_provider(p_id)
        for m in models:
            if m.selectable:
                gen_tok = generate_model_token(p_id, m.model_id).lower()
                if gen_tok == token_clean:
                    return m.model_id
        return None
    else:
        token_clean = arg1.strip().lower()
        for p_id in list_registered_providers():
            models = get_models_for_provider(p_id)
            for m in models:
                if m.selectable:
                    gen_tok = generate_model_token(p_id, m.model_id).lower()
                    if gen_tok == token_clean:
                        return (p_id, m.model_id)
        return None


def validate_model_for_provider(provider_id: str, model_id: str) -> bool:
    """Check if model_id is valid and selectable for the given provider."""
    p_id = provider_id.lower().strip()
    m_clean = model_id.strip()
    models = get_models_for_provider(p_id)
    if not models:
        # If catalog is empty, allow configured default model
        desc = get_descriptor(p_id)
        return bool(desc and desc.default_model == m_clean)

    return any(m.model_id == m_clean and m.selectable for m in models)
