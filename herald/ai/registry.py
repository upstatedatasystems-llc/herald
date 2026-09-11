"""
Authoritative Provider Registry for Herald AI Architecture.
Maintains canonical provider descriptors, credential requirements, configuration status,
and non-cached per-job provider instantiation.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from herald.ai.base import AIProvider
from herald.ai.capabilities import AIModelCapabilities, ProviderCapabilities
from herald.config import settings


@dataclass
class ProviderDescriptor:
    """Authoritative descriptor representing a supported AI provider."""

    provider_id: str
    display_name: str
    credential_names: list[str]
    default_model: str
    factory: Callable[[str | None], AIProvider]
    capabilities: ProviderCapabilities
    is_configured_fn: Callable[[], bool]
    supports_model_discovery: bool = False
    catalog_models: list[AIModelCapabilities] = field(default_factory=list)

    def is_configured(self) -> bool:
        return self.is_configured_fn()


_REGISTRY: dict[str, ProviderDescriptor] = {}


def register_provider(descriptor: ProviderDescriptor) -> None:
    _REGISTRY[descriptor.provider_id.lower().strip()] = descriptor


def get_descriptor(provider_id: str | None) -> ProviderDescriptor | None:
    if not provider_id:
        return None
    return _REGISTRY.get(provider_id.lower().strip())


get_provider_descriptor = get_descriptor



def list_descriptors() -> list[ProviderDescriptor]:
    return list(_REGISTRY.values())


def list_registered_providers() -> dict[str, ProviderDescriptor]:
    return dict(_REGISTRY)


def is_provider_registered(provider_id: str | None) -> bool:
    if not provider_id:
        return False
    return provider_id.lower().strip() in _REGISTRY



def is_provider_configured(provider_id: str | None) -> bool:
    desc = get_descriptor(provider_id)
    return desc.is_configured() if desc else False


def get_default_model(provider_id: str | None) -> str:
    desc = get_descriptor(provider_id)
    if not desc:
        return ""
    # Check if there is a provider-specific setting default first
    p_id = desc.provider_id
    if p_id == "gemini":
        return getattr(settings, "GEMINI_MODEL", "gemini-3.5-flash")
    if p_id == "groq":
        return getattr(settings, "GROQ_MODEL", "groq/compound")
    if p_id == "cloudflare":
        return settings.effective_cloudflare_ai_model
    if p_id == "openai":
        return getattr(settings, "OPENAI_MODEL", "gpt-4o")
    if p_id == "openrouter":
        return getattr(settings, "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")
    if p_id == "mistral":
        return getattr(settings, "MISTRAL_MODEL", "mistral-large-latest")
    if p_id == "anthropic":
        return getattr(settings, "ANTHROPIC_MODEL", "claude-3-7-sonnet-20250219")
    if p_id == "ollama":
        return getattr(settings, "OLLAMA_MODEL", "llama3.2")
    return desc.default_model


def create_provider(provider_id: str | None, model_id: str | None = None) -> AIProvider:
    """
    Instantiate a new AIProvider for job execution without global mutable caching.
    Uses specified model_id, or falls back to provider default model.
    """
    p_id = (provider_id or "literal").lower().strip()
    desc = get_descriptor(p_id)
    if not desc:
        from herald.ai.literal_provider import LiteralProvider
        return LiteralProvider()

    eff_model = model_id or get_default_model(p_id)
    return desc.factory(eff_model)


def validate_server_default_chain(
    primary: str | None,
    secondary: str | None = None,
    tertiary: str | None = None,
) -> tuple[bool, str | None]:
    """
    Validate server default chain uniqueness and slot constraints at startup.
    Returns (is_valid, error_message).
    """
    p_clean = (primary or "").lower().strip()
    s_clean = (secondary or "").lower().strip() if secondary else None
    t_clean = (tertiary or "").lower().strip() if tertiary else None

    if not p_clean:
        return False, "Primary AI provider cannot be empty"

    if p_clean not in _REGISTRY:
        return False, f"Primary provider '{primary}' is not registered"

    if s_clean:
        if s_clean not in _REGISTRY:
            return False, f"Secondary provider '{secondary}' is not registered"
        if s_clean == p_clean:
            return False, f"Secondary provider '{secondary}' duplicates Primary"
        if s_clean == "literal":
            return False, "Literal mode cannot be a secondary failover provider"

    if t_clean:
        if not s_clean:
            return False, "Tertiary provider cannot be configured without a Secondary provider"
        if t_clean not in _REGISTRY:
            return False, f"Tertiary provider '{tertiary}' is not registered"
        if t_clean == p_clean:
            return False, f"Tertiary provider '{tertiary}' duplicates Primary"
        if t_clean == s_clean:
            return False, f"Tertiary provider '{tertiary}' duplicates Secondary"
        if t_clean == "literal":
            return False, "Literal mode cannot be a tertiary failover provider"

    return True, None


# Factory helper closures to avoid circular imports
def _create_gemini(model: str | None) -> AIProvider:
    from herald.ai.gemini_provider import GeminiProvider
    return GeminiProvider(model_name=model)


def _create_groq(model: str | None) -> AIProvider:
    from herald.ai.groq_provider import GroqProvider
    return GroqProvider(model=model)


def _create_cloudflare(model: str | None) -> AIProvider:
    from herald.ai.cloudflare_provider import CloudflareProvider
    return CloudflareProvider(model=model)


def _create_openai(model: str | None) -> AIProvider:
    from herald.ai.openai_provider import OpenAIProvider
    return OpenAIProvider(model=model)


def _create_openrouter(model: str | None) -> AIProvider:
    from herald.ai.openrouter_provider import OpenRouterProvider
    return OpenRouterProvider(model=model)


def _create_mistral(model: str | None) -> AIProvider:
    from herald.ai.mistral_provider import MistralProvider
    return MistralProvider(model=model)


def _create_anthropic(model: str | None) -> AIProvider:
    from herald.ai.anthropic_provider import AnthropicProvider
    return AnthropicProvider(model=model)


def _create_ollama(model: str | None) -> AIProvider:
    from herald.ai.ollama_provider import OllamaProvider
    return OllamaProvider(model=model)


def _create_literal(model: str | None = None) -> AIProvider:
    from herald.ai.literal_provider import LiteralProvider
    return LiteralProvider()


# Register all known providers
register_provider(
    ProviderDescriptor(
        provider_id="gemini",
        display_name="Gemini",
        credential_names=["GEMINI_API_KEY"],
        default_model="gemini-3.5-flash",
        factory=_create_gemini,
        capabilities=ProviderCapabilities(
            script_brief=True,
            script_standard=True,
            structured_output=True,
            research_grounding=True,
            url_context_extraction=True,
            verification=True,
            usage_metrics=True,
        ),
        is_configured_fn=lambda: bool(settings.GEMINI_API_KEY and settings.GEMINI_API_KEY.strip()),
        supports_model_discovery=True,
        catalog_models=[
            AIModelCapabilities(
                provider_id="gemini",
                model_id="gemini-3.5-flash",
                display_name="Gemini 3.5 Flash",
                context_window=1_048_576,
                max_output=16384,
            ),
            AIModelCapabilities(
                provider_id="gemini",
                model_id="gemini-3.6-flash",
                display_name="Gemini 3.6 Flash (Research)",
                context_window=1_048_576,
                max_output=16384,
            ),
            AIModelCapabilities(
                provider_id="gemini",
                model_id="gemini-2.5-flash",
                display_name="Gemini 2.5 Flash",
                context_window=1_048_576,
                max_output=8192,
            ),
        ],
    )
)

register_provider(
    ProviderDescriptor(
        provider_id="groq",
        display_name="Groq",
        credential_names=["GROQ_API_KEY"],
        default_model="llama-3.3-70b-versatile",
        factory=_create_groq,
        capabilities=ProviderCapabilities(
            script_brief=True,
            script_standard=True,
            structured_output=True,
            research_grounding=False,
            url_context_extraction=False,
            verification=True,
            usage_metrics=True,
        ),
        is_configured_fn=lambda: bool(settings.GROQ_API_KEY and settings.GROQ_API_KEY.strip()),
        supports_model_discovery=True,
        catalog_models=[
            AIModelCapabilities(
                provider_id="groq",
                model_id="groq/compound",
                display_name="Groq Compound",
                context_window=131_072,
                max_output=8192,
            ),
            AIModelCapabilities(
                provider_id="groq",
                model_id="groq/compound-mini",
                display_name="Groq Compound Mini",
                context_window=131_072,
                max_output=8192,
            ),
            AIModelCapabilities(
                provider_id="groq",
                model_id="llama-3.3-70b-versatile",
                display_name="Llama 3.3 70B Versatile",
                context_window=131_072,
                max_output=8192,
            ),
            AIModelCapabilities(
                provider_id="groq",
                model_id="openai/gpt-oss-120b",
                display_name="GPT-OSS 120B",
                context_window=131_072,
                max_output=8192,
            ),
            AIModelCapabilities(
                provider_id="groq",
                model_id="openai/gpt-oss-20b",
                display_name="GPT-OSS 20B",
                context_window=131_072,
                max_output=8192,
            ),
        ],
    )
)

register_provider(
    ProviderDescriptor(
        provider_id="cloudflare",
        display_name="Cloudflare Workers AI",
        credential_names=["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"],
        default_model="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        factory=_create_cloudflare,
        capabilities=ProviderCapabilities(
            script_brief=True,
            script_standard=True,
            structured_output=True,
            research_grounding=False,
            url_context_extraction=False,
            verification=True,
            usage_metrics=True,
        ),
        is_configured_fn=lambda: bool(
            settings.CLOUDFLARE_API_TOKEN
            and settings.CLOUDFLARE_API_TOKEN.strip()
            and settings.CLOUDFLARE_ACCOUNT_ID
            and settings.CLOUDFLARE_ACCOUNT_ID.strip()
        ),
        supports_model_discovery=True,
        catalog_models=[
            AIModelCapabilities(
                provider_id="cloudflare",
                model_id="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                display_name="Llama 3.3 70B Instruct (Fast)",
                context_window=131_072,
                max_output=8192,
            ),
            AIModelCapabilities(
                provider_id="cloudflare",
                model_id="@cf/qwen/qwen3.8-27b",
                display_name="Qwen 3.8 27B",
                context_window=32_768,
                max_output=16384,
                model_specific_defaults={"reasoning_effort": "low", "max_completion_tokens": 16384},
            ),
            AIModelCapabilities(
                provider_id="cloudflare",
                model_id="@cf/google/gemma-4-26b-a4b-it",
                display_name="Gemma 4 26B A4B IT",
                context_window=32_768,
                max_output=16384,
                model_specific_defaults={"reasoning_effort": "low", "max_completion_tokens": 16384},
            ),
            AIModelCapabilities(
                provider_id="cloudflare",
                model_id="@cf/zai-org/glm-4.7-flash",
                display_name="GLM 4.7 Flash",
                context_window=32_768,
                max_output=8192,
            ),
        ],
    )
)

register_provider(
    ProviderDescriptor(
        provider_id="openai",
        display_name="OpenAI",
        credential_names=["OPENAI_API_KEY"],
        default_model="gpt-4o",
        factory=_create_openai,
        capabilities=ProviderCapabilities(
            script_brief=True,
            script_standard=True,
            structured_output=True,
            research_grounding=False,
            url_context_extraction=False,
            verification=True,
            usage_metrics=True,
        ),
        is_configured_fn=lambda: bool(settings.OPENAI_API_KEY and settings.OPENAI_API_KEY.strip()),
        supports_model_discovery=True,
        catalog_models=[
            AIModelCapabilities(
                provider_id="openai",
                model_id="gpt-4o",
                display_name="GPT-4o",
                context_window=128_000,
                max_output=16384,
            ),
            AIModelCapabilities(
                provider_id="openai",
                model_id="gpt-4o-mini",
                display_name="GPT-4o Mini",
                context_window=128_000,
                max_output=16384,
            ),
        ],
    )
)

register_provider(
    ProviderDescriptor(
        provider_id="openrouter",
        display_name="OpenRouter",
        credential_names=["OPENROUTER_API_KEY"],
        default_model="meta-llama/llama-3.3-70b-instruct",
        factory=_create_openrouter,
        capabilities=ProviderCapabilities(
            script_brief=True,
            script_standard=True,
            structured_output=True,
            research_grounding=False,
            url_context_extraction=False,
            verification=True,
            usage_metrics=True,
        ),
        is_configured_fn=lambda: bool(settings.OPENROUTER_API_KEY and settings.OPENROUTER_API_KEY.strip()),
        supports_model_discovery=False,
        catalog_models=[
            AIModelCapabilities(
                provider_id="openrouter",
                model_id="meta-llama/llama-3.3-70b-instruct",
                display_name="Llama 3.3 70B Instruct",
                context_window=131_072,
                max_output=8192,
            ),
        ],
    )
)

register_provider(
    ProviderDescriptor(
        provider_id="mistral",
        display_name="Mistral",
        credential_names=["MISTRAL_API_KEY"],
        default_model="mistral-large-latest",
        factory=_create_mistral,
        capabilities=ProviderCapabilities(
            script_brief=True,
            script_standard=True,
            structured_output=True,
            research_grounding=False,
            url_context_extraction=False,
            verification=True,
            usage_metrics=True,
        ),
        is_configured_fn=lambda: bool(settings.MISTRAL_API_KEY and settings.MISTRAL_API_KEY.strip()),
        supports_model_discovery=False,
        catalog_models=[
            AIModelCapabilities(
                provider_id="mistral",
                model_id="mistral-large-latest",
                display_name="Mistral Large",
                context_window=128_000,
                max_output=8192,
            ),
        ],
    )
)

register_provider(
    ProviderDescriptor(
        provider_id="anthropic",
        display_name="Anthropic",
        credential_names=["ANTHROPIC_API_KEY"],
        default_model="claude-3-7-sonnet-20250219",
        factory=_create_anthropic,
        capabilities=ProviderCapabilities(
            script_brief=True,
            script_standard=True,
            structured_output=True,
            research_grounding=False,
            url_context_extraction=False,
            verification=True,
            usage_metrics=True,
        ),
        is_configured_fn=lambda: bool(settings.ANTHROPIC_API_KEY and settings.ANTHROPIC_API_KEY.strip()),
        supports_model_discovery=False,
        catalog_models=[
            AIModelCapabilities(
                provider_id="anthropic",
                model_id="claude-3-7-sonnet-20250219",
                display_name="Claude 3.7 Sonnet",
                context_window=200_000,
                max_output=8192,
            ),
        ],
    )
)

register_provider(
    ProviderDescriptor(
        provider_id="ollama",
        display_name="Ollama",
        credential_names=[],
        default_model="llama3.2",
        factory=_create_ollama,
        capabilities=ProviderCapabilities(
            script_brief=True,
            script_standard=True,
            structured_output=True,
            research_grounding=False,
            url_context_extraction=False,
            verification=True,
            usage_metrics=True,
        ),
        is_configured_fn=lambda: bool(getattr(settings, "OLLAMA_BASE_URL", "")),
        supports_model_discovery=False,
        catalog_models=[
            AIModelCapabilities(
                provider_id="ollama",
                model_id="llama3.2",
                display_name="Llama 3.2 (Local)",
                context_window=131_072,
                max_output=8192,
            ),
        ],
    )
)

register_provider(
    ProviderDescriptor(
        provider_id="literal",
        display_name="Literal (Zero AI)",
        credential_names=[],
        default_model="none",
        factory=_create_literal,
        capabilities=ProviderCapabilities(
            script_brief=True,
            script_standard=True,
            structured_output=False,
            research_grounding=False,
            url_context_extraction=False,
            verification=False,
            usage_metrics=False,
        ),
        is_configured_fn=lambda: True,
        supports_model_discovery=False,
        catalog_models=[],
    )
)
