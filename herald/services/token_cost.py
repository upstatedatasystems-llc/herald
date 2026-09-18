"""
Token usage and cost accounting service for Herald.
Provides truthful aggregation of token usage and exact-match model cost estimation.
"""

from dataclasses import dataclass
from typing import Any, Iterable

from herald.db.models import AIInteraction


@dataclass(frozen=True)
class ModelPricing:
    """Pricing rates per 1,000,000 tokens in USD with reference metadata."""
    prompt_per_m: float
    completion_per_m: float
    pricing_type: str = "list_price_reference"
    effective_date: str = "2024-10-01"
    provenance: str = "vendor_docs"
    verified_at: str | None = None
    is_verified: bool = False


# Canonical pricing table keyed strictly by exact lowercase (provider_id, model_id) tuples.
# Reference list prices (USD per 1M tokens). Not enabled by default.
# Groq Compound systems are excluded: compound tools/models cannot be represented by a static rate.
PRICING_TABLE: dict[tuple[str, str], ModelPricing] = {
    # Gemini
    ("gemini", "gemini-2.5-flash"): ModelPricing(0.075, 0.30, pricing_type="list_price_reference", effective_date="2024-10-01", provenance="https://ai.google.dev/pricing", verified_at="2026-09-18", is_verified=False),
    ("gemini", "gemini-3.5-flash"): ModelPricing(0.075, 0.30, pricing_type="list_price_reference", effective_date="2025-01-01", provenance="https://ai.google.dev/pricing", verified_at="2026-09-18", is_verified=False),
    ("gemini", "gemini-3.6-flash"): ModelPricing(0.075, 0.30, pricing_type="list_price_reference", effective_date="2025-01-01", provenance="https://ai.google.dev/pricing", verified_at="2026-09-18", is_verified=False),
    ("gemini", "gemini-1.5-flash"): ModelPricing(0.075, 0.30, pricing_type="list_price_reference", effective_date="2024-05-14", provenance="https://ai.google.dev/pricing", verified_at="2026-09-18", is_verified=False),
    ("gemini", "gemini-1.5-pro"): ModelPricing(1.25, 5.00, pricing_type="list_price_reference", effective_date="2024-05-14", provenance="https://ai.google.dev/pricing", verified_at="2026-09-18", is_verified=False),

    # Groq (rates per 1M tokens) - Groq compound models removed (cannot be estimated by static token rates)
    ("groq", "llama-3.3-70b-versatile"): ModelPricing(0.59, 0.79, pricing_type="list_price_reference", effective_date="2024-12-06", provenance="https://groq.com/pricing", verified_at="2026-09-18", is_verified=False),
    ("groq", "openai/gpt-oss-120b"): ModelPricing(0.60, 0.80, pricing_type="list_price_reference", effective_date="2025-01-01", provenance="https://groq.com/pricing", verified_at="2026-09-18", is_verified=False),
    ("groq", "openai/gpt-oss-20b"): ModelPricing(0.15, 0.15, pricing_type="list_price_reference", effective_date="2025-01-01", provenance="https://groq.com/pricing", verified_at="2026-09-18", is_verified=False),

    # OpenAI
    ("openai", "gpt-4o"): ModelPricing(2.50, 10.00, pricing_type="list_price_reference", effective_date="2024-05-13", provenance="https://openai.com/api/pricing", verified_at="2026-09-18", is_verified=False),
    ("openai", "gpt-4o-mini"): ModelPricing(0.15, 0.60, pricing_type="list_price_reference", effective_date="2024-07-18", provenance="https://openai.com/api/pricing", verified_at="2026-09-18", is_verified=False),

    # Anthropic
    ("anthropic", "claude-3-7-sonnet-20250219"): ModelPricing(3.00, 15.00, pricing_type="list_price_reference", effective_date="2025-02-19", provenance="https://anthropic.com/pricing", verified_at="2026-09-18", is_verified=False),
    ("anthropic", "claude-3-5-sonnet-20241022"): ModelPricing(3.00, 15.00, pricing_type="list_price_reference", effective_date="2024-10-22", provenance="https://anthropic.com/pricing", verified_at="2026-09-18", is_verified=False),
    ("anthropic", "claude-3-5-haiku-20241022"): ModelPricing(0.80, 4.00, pricing_type="list_price_reference", effective_date="2024-10-22", provenance="https://anthropic.com/pricing", verified_at="2026-09-18", is_verified=False),

    # Mistral
    ("mistral", "mistral-large-latest"): ModelPricing(2.00, 6.00, pricing_type="list_price_reference", effective_date="2024-11-18", provenance="https://mistral.ai/technology/#pricing", verified_at="2026-09-18", is_verified=False),

    # Cloudflare Workers AI
    ("cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"): ModelPricing(0.35, 0.40, pricing_type="list_price_reference", effective_date="2024-12-01", provenance="https://developers.cloudflare.com/workers-ai/models", verified_at="2026-09-18", is_verified=False),
    ("cloudflare", "@cf/qwen/qwen3.8-27b"): ModelPricing(0.25, 0.30, pricing_type="list_price_reference", effective_date="2024-12-01", provenance="https://developers.cloudflare.com/workers-ai/models", verified_at="2026-09-18", is_verified=False),
    ("cloudflare", "@cf/google/gemma-4-26b-a4b-it"): ModelPricing(0.25, 0.30, pricing_type="list_price_reference", effective_date="2024-12-01", provenance="https://developers.cloudflare.com/workers-ai/models", verified_at="2026-09-18", is_verified=False),
    ("cloudflare", "@cf/zai-org/glm-4.7-flash"): ModelPricing(0.15, 0.20, pricing_type="list_price_reference", effective_date="2024-12-01", provenance="https://developers.cloudflare.com/workers-ai/models", verified_at="2026-09-18", is_verified=False),

    # OpenRouter
    ("openrouter", "meta-llama/llama-3.3-70b-instruct"): ModelPricing(0.40, 0.40, pricing_type="list_price_reference", effective_date="2024-12-06", provenance="https://openrouter.ai/models", verified_at="2026-09-18", is_verified=False),

    # Ollama / Local AI models are free ($0.00)
    ("ollama", "llama3.2"): ModelPricing(0.0, 0.0, pricing_type="local_execution", effective_date="2024-09-25", provenance="local_execution", verified_at="2026-09-18", is_verified=True),
}


@dataclass
class JobTokenAndCostSummary:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    total_cost_usd: float
    is_cost_complete: bool  # True if every interaction with tokens had known pricing
    is_cost_available: bool  # True if at least one interaction had known pricing or 0 calls
    call_count: int
    by_model: dict[str, dict[str, Any]]

    @property
    def cost_display(self) -> str:
        """
        Return truthful cost string:
        - '$0.00' if 0 interactions or only local models
        - '~$0.08 est.' if complete
        - '~$0.0042 (partial)' if incomplete but some pricing available
        - 'unavailable' if interactions occurred but no pricing exists
        """
        if self.call_count == 0:
            return "$0.00"
        if not self.is_cost_available:
            return "unavailable"
        if not self.is_cost_complete:
            if self.total_cost_usd < 0.01:
                return f"~${self.total_cost_usd:.4f} (partial)"
            return f"~${self.total_cost_usd:.2f} (partial)"
        if self.total_cost_usd == 0.0:
            return "$0.00"
        if self.total_cost_usd < 0.01:
            return f"~${self.total_cost_usd:.4f} est."
        return f"~${self.total_cost_usd:.2f} est."

    @property
    def tokens_display(self) -> str:
        """Formatted token count, e.g. '12,450 tokens' or '0 tokens'."""
        return f"{self.total_tokens:,} tokens"


def is_external_billable_provider(provider: str | None) -> bool:
    """Return True if provider is an external paid API provider rather than local/internal."""
    p = (provider or "").strip().lower()
    return p in ("gemini", "groq", "openai", "anthropic", "mistral", "cloudflare", "cloudflare_workers_ai", "openrouter")


def get_effective_pricing_table() -> dict[tuple[str, str], ModelPricing]:
    """
    Return effective pricing table, merging base PRICING_TABLE with any configured overrides.
    Honors HERALD_ENABLE_BUILTIN_EXTERNAL_PRICING (default False). When disabled, only local
    zero-cost models and explicit overrides are priced.
    """
    from herald.config import settings
    enable_builtin_external = getattr(settings, "HERALD_ENABLE_BUILTIN_EXTERNAL_PRICING", False)

    table: dict[tuple[str, str], ModelPricing] = {}
    for k, v in PRICING_TABLE.items():
        prov, mod = k
        is_local = (prov in ("ollama", "local")) or (v.prompt_per_m == 0.0 and v.completion_per_m == 0.0)
        if is_local or enable_builtin_external:
            table[k] = v

    try:
        raw_overrides = getattr(settings, "HERALD_MODEL_PRICING_OVERRIDES_JSON", "") or ""
        if raw_overrides.strip():
            import json
            parsed = json.loads(raw_overrides)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if "/" in k:
                        prov, m = k.split("/", 1)
                    elif ":" in k:
                        prov, m = k.split(":", 1)
                    elif "," in k:
                        parts = k.strip("() ").split(",")
                        prov, m = parts[0].strip(), parts[1].strip()
                    else:
                        continue
                    prov_clean = prov.strip().lower()
                    model_clean = m.strip().lower()
                    if isinstance(v, dict):
                        p_rate = float(v.get("prompt_per_m", 0.0))
                        c_rate = float(v.get("completion_per_m", 0.0))
                        eff_date = str(v.get("effective_date", "override"))
                        table[(prov_clean, model_clean)] = ModelPricing(
                            prompt_per_m=p_rate,
                            completion_per_m=c_rate,
                            pricing_type="operator_override",
                            effective_date=eff_date,
                            provenance=str(v.get("provenance", "operator_configured")),
                            verified_at=str(v.get("verified_at", "operator_configured")),
                            is_verified=True,
                        )
    except Exception:
        pass
    return table


def calculate_interaction_cost(interaction: AIInteraction) -> tuple[float | None, bool]:
    """
    Calculate USD cost for a single AIInteraction.
    Returns (cost_usd, is_known).
    If provider/model is not in effective pricing table, returns (None, False).
    """
    provider = (interaction.provider or "").strip().lower()
    model = (interaction.model or "").strip().lower()
    pricing = get_effective_pricing_table().get((provider, model))
    if not pricing:
        return None, False

    prompt_tok = interaction.prompt_tokens or 0
    comp_tok = interaction.completion_tokens or 0

    # Only fall back to total_tokens if rates are symmetric; do not guess asymmetric rates
    if prompt_tok == 0 and comp_tok == 0 and interaction.total_tokens:
        if pricing.prompt_per_m == pricing.completion_per_m:
            cost = (interaction.total_tokens / 1_000_000.0) * pricing.prompt_per_m
            return cost, True
        return None, False

    cost = (prompt_tok / 1_000_000.0) * pricing.prompt_per_m + (comp_tok / 1_000_000.0) * pricing.completion_per_m
    return cost, True


def aggregate_job_tokens_and_cost(interactions: Iterable[AIInteraction]) -> JobTokenAndCostSummary:
    """
    Aggregate token counts and compute cost for a collection of AIInteractions.
    Never returns $0.00 if non-local API interactions occurred without known rates or missing telemetry.
    """
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    total_cost = 0.0
    known_cost_token_bearing_interactions = 0
    total_token_bearing_interactions = 0
    external_missing_telemetry_interactions = 0
    total_interactions = 0
    by_model: dict[str, dict[str, Any]] = {}

    for inter in interactions:
        total_interactions += 1
        p_tok = inter.prompt_tokens or 0
        c_tok = inter.completion_tokens or 0
        t_tok = inter.total_tokens or (p_tok + c_tok)

        prompt_tokens += p_tok
        completion_tokens += c_tok
        total_tokens += t_tok

        is_token_bearing = (t_tok > 0)
        if is_token_bearing:
            total_token_bearing_interactions += 1
        else:
            # 0 tokens observed: check if this is an external billable provider with missing telemetry
            prov = (inter.provider or "").strip().lower()
            if is_external_billable_provider(prov):
                external_missing_telemetry_interactions += 1

        cost, is_known = calculate_interaction_cost(inter)
        if is_known and cost is not None:
            total_cost += cost
            if is_token_bearing:
                known_cost_token_bearing_interactions += 1

        key = f"{inter.provider}:{inter.model}"
        if key not in by_model:
            by_model[key] = {
                "provider": inter.provider,
                "model": inter.model,
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0 if is_known else None,
            }
        by_model[key]["calls"] += 1
        by_model[key]["prompt_tokens"] += p_tok
        by_model[key]["completion_tokens"] += c_tok
        by_model[key]["total_tokens"] += t_tok
        if is_known and cost is not None and by_model[key]["cost_usd"] is not None:
            by_model[key]["cost_usd"] += cost

    if total_interactions == 0:
        return JobTokenAndCostSummary(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            total_cost_usd=0.0,
            is_cost_complete=True,
            is_cost_available=True,
            call_count=0,
            by_model={},
        )

    # Truthful cost availability and completeness logic
    if total_token_bearing_interactions == 0:
        if external_missing_telemetry_interactions > 0:
            # External billable calls were made, but token telemetry is missing: cost is unavailable
            is_cost_complete = False
            is_cost_available = False
        else:
            # Only genuinely zero-cost local or internal non-billable interactions took place
            is_cost_complete = True
            is_cost_available = True
    else:
        # Some token-bearing interactions exist.
        # If external calls with missing telemetry occurred, overall cost is incomplete/partial
        if external_missing_telemetry_interactions > 0:
            is_cost_complete = False
        else:
            is_cost_complete = (known_cost_token_bearing_interactions == total_token_bearing_interactions)
        is_cost_available = (known_cost_token_bearing_interactions > 0)

    return JobTokenAndCostSummary(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        total_cost_usd=total_cost,
        is_cost_complete=is_cost_complete,
        is_cost_available=is_cost_available,
        call_count=total_interactions,
        by_model=by_model,
    )
