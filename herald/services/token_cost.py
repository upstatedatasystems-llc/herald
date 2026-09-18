"""
Token usage and cost accounting service for Herald.
Provides truthful aggregation of token usage and exact-match model cost estimation.
"""

from dataclasses import dataclass
from typing import Any, Iterable

from herald.db.models import AIInteraction


@dataclass(frozen=True)
class ModelPricing:
    """Pricing rates per 1,000,000 tokens in USD with effective rate date."""
    prompt_per_m: float
    completion_per_m: float
    effective_date: str = "2024-10-01"


# Canonical pricing table keyed strictly by exact lowercase (provider_id, model_id) tuples.
# Rates in USD per 1M tokens.
PRICING_TABLE: dict[tuple[str, str], ModelPricing] = {
    # Gemini
    ("gemini", "gemini-2.5-flash"): ModelPricing(0.075, 0.30, effective_date="2024-10-01"),
    ("gemini", "gemini-3.5-flash"): ModelPricing(0.075, 0.30, effective_date="2025-01-01"),
    ("gemini", "gemini-3.6-flash"): ModelPricing(0.075, 0.30, effective_date="2025-01-01"),
    ("gemini", "gemini-1.5-flash"): ModelPricing(0.075, 0.30, effective_date="2024-05-14"),
    ("gemini", "gemini-1.5-pro"): ModelPricing(1.25, 5.00, effective_date="2024-05-14"),

    # Groq (rates per 1M tokens)
    ("groq", "llama-3.3-70b-versatile"): ModelPricing(0.59, 0.79, effective_date="2024-12-06"),
    ("groq", "groq/compound"): ModelPricing(0.59, 0.79, effective_date="2025-01-01"),
    ("groq", "groq/compound-mini"): ModelPricing(0.20, 0.20, effective_date="2025-01-01"),
    ("groq", "openai/gpt-oss-120b"): ModelPricing(0.60, 0.80, effective_date="2025-01-01"),
    ("groq", "openai/gpt-oss-20b"): ModelPricing(0.15, 0.15, effective_date="2025-01-01"),

    # OpenAI
    ("openai", "gpt-4o"): ModelPricing(2.50, 10.00, effective_date="2024-05-13"),
    ("openai", "gpt-4o-mini"): ModelPricing(0.15, 0.60, effective_date="2024-07-18"),

    # Anthropic
    ("anthropic", "claude-3-7-sonnet-20250219"): ModelPricing(3.00, 15.00, effective_date="2025-02-19"),
    ("anthropic", "claude-3-5-sonnet-20241022"): ModelPricing(3.00, 15.00, effective_date="2024-10-22"),
    ("anthropic", "claude-3-5-haiku-20241022"): ModelPricing(0.80, 4.00, effective_date="2024-10-22"),

    # Mistral
    ("mistral", "mistral-large-latest"): ModelPricing(2.00, 6.00, effective_date="2024-11-18"),

    # Cloudflare Workers AI
    ("cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"): ModelPricing(0.35, 0.40, effective_date="2024-12-01"),
    ("cloudflare", "@cf/qwen/qwen3.8-27b"): ModelPricing(0.25, 0.30, effective_date="2024-12-01"),
    ("cloudflare", "@cf/google/gemma-4-26b-a4b-it"): ModelPricing(0.25, 0.30, effective_date="2024-12-01"),
    ("cloudflare", "@cf/zai-org/glm-4.7-flash"): ModelPricing(0.15, 0.20, effective_date="2024-12-01"),

    # OpenRouter
    ("openrouter", "meta-llama/llama-3.3-70b-instruct"): ModelPricing(0.40, 0.40, effective_date="2024-12-06"),

    # Ollama / Local AI models are free ($0.00)
    ("ollama", "llama3.2"): ModelPricing(0.0, 0.0, effective_date="2024-09-25"),
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
        - '$0.0042' if complete
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
            return f"${self.total_cost_usd:.4f}"
        return f"${self.total_cost_usd:.2f}"

    @property
    def tokens_display(self) -> str:
        """Formatted token count, e.g. '12,450 tokens' or '0 tokens'."""
        return f"{self.total_tokens:,} tokens"


def calculate_interaction_cost(interaction: AIInteraction) -> tuple[float | None, bool]:
    """
    Calculate USD cost for a single AIInteraction.
    Returns (cost_usd, is_known).
    If provider/model is not in PRICING_TABLE, returns (None, False).
    """
    provider = (interaction.provider or "").strip().lower()
    model = (interaction.model or "").strip().lower()
    pricing = PRICING_TABLE.get((provider, model))
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
    Never returns $0.00 if non-local API interactions occurred without known rates.
    """
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    total_cost = 0.0
    known_cost_token_bearing_interactions = 0
    total_token_bearing_interactions = 0
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

    # If no calls had tokens, consider complete if all had known pricing models
    if total_token_bearing_interactions == 0:
        is_cost_complete = True
        is_cost_available = True
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
