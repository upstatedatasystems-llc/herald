"""
Central Settings Resolution Service for Herald AI Architecture.
Computes authoritative, typed ResolvedJobSettings with strict precedence:
explicit request override > user preference > server/application default.
"""

from dataclasses import dataclass, field
from typing import Any

from herald.ai.catalog import validate_model_for_provider
from herald.ai.errors import AIProviderError
from herald.ai.registry import (
    get_default_model,
    get_descriptor,
    validate_server_default_chain,
)
from herald.config import settings as global_settings
from herald.db.models import RequestMode


@dataclass(frozen=True)
class AIProviderCandidate:
    """An immutable (provider_id, model_id) execution candidate in a job's chain."""

    provider_id: str
    model_id: str

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider_id, "model": self.model_id}


@dataclass(frozen=True)
class ResolvedJobSettings:
    """Authoritative resolved settings for job intake, creation, execution, and rerun."""

    mode: str
    research_depth: str | None
    voice: str
    speed: float
    custom_title: str | None
    chunk_chars: int
    verify: bool
    ai_candidates: list[AIProviderCandidate] = field(default_factory=list)
    research_provider: str | None = None
    research_model: str | None = None

    @property
    def primary_candidate(self) -> AIProviderCandidate:
        if self.ai_candidates:
            return self.ai_candidates[0]
        return AIProviderCandidate(provider_id="literal", model_id="none")

    def to_snapshot(self) -> dict[str, Any]:
        """Convert to canonical generation_settings_json snapshot format."""
        return {
            "mode": self.mode,
            "research_depth": self.research_depth,
            "voice": self.voice,
            "speed": self.speed,
            "custom_title": self.custom_title,
            "chunk_chars": self.chunk_chars,
            "verify": self.verify,
            "ai_provider": self.primary_candidate.provider_id,
            "ai_model": self.primary_candidate.model_id,
            "ai_provider_chain": [c.to_dict() for c in self.ai_candidates],
            "research_provider": self.research_provider,
            "research_model": self.research_model,
        }


def get_server_default_chain(cfg: Any = None) -> list[str]:
    """
    Resolve the server default ordered provider chain:
    AI_PROVIDER -> AI_SECONDARY_PROVIDER -> AI_TERTIARY_PROVIDER
    Validates uniqueness and eliminates unconfigured slots.
    """
    conf = cfg or global_settings
    primary = (getattr(conf, "AI_PROVIDER", "gemini") or "gemini").lower().strip()
    if primary == "none":
        primary = "literal"
    secondary = getattr(conf, "AI_SECONDARY_PROVIDER", None)
    if secondary and secondary.lower().strip() == "none":
        secondary = None
    tertiary = getattr(conf, "AI_TERTIARY_PROVIDER", None)
    if tertiary and tertiary.lower().strip() == "none":
        tertiary = None

    is_valid, err_msg = validate_server_default_chain(primary, secondary, tertiary)
    if not is_valid:
        raise ValueError(f"Invalid server default AI provider chain: {err_msg}")

    chain = [primary]
    if secondary:
        s_clean = secondary.lower().strip()
        if s_clean not in chain:
            chain.append(s_clean)
    if tertiary:
        t_clean = tertiary.lower().strip()
        if t_clean not in chain:
            chain.append(t_clean)

    return chain


def resolve_job_settings(
    request_params: dict[str, Any] | None = None,
    user_prefs: dict[str, Any] | None = None,
    server_cfg: Any = None,
) -> ResolvedJobSettings:
    """
    Centrally resolve job settings with strict precedence:
    explicit request override > user preference > server default.
    Ensures complete, frozen provider candidate chain is constructed before any AI execution.
    """
    req = request_params or {}
    usr = user_prefs or {}
    cfg = server_cfg or global_settings

    # 1. Resolve Mode
    req_mode_raw = req.get("mode") or req.get("request_mode")
    usr_mode_raw = usr.get("default_mode")
    mode_cand: str | None = None
    if req_mode_raw:
        mode_cand = str(req_mode_raw).lower().strip()
    elif usr_mode_raw:
        mode_cand = str(usr_mode_raw).lower().strip()

    # Handle legacy 'detailed' alias
    if mode_cand == "detailed":
        mode_cand = RequestMode.RESEARCH.value

    # Research depth
    research_depth: str | None = None
    if mode_cand == RequestMode.RESEARCH.value:
        depth_raw = req.get("research_depth") or usr.get("default_research_depth") or "medium"
        research_depth = str(depth_raw).lower().strip()

    # 2. Resolve Voice
    eff_voice = (
        req.get("voice")
        or usr.get("default_voice")
        or getattr(cfg, "KOKORO_VOICE", "af_heart")
    ).strip()

    # 3. Resolve Speed
    raw_speed = req.get("speed") if req.get("speed") is not None else usr.get("default_speed")
    if raw_speed is None:
        raw_speed = getattr(cfg, "KOKORO_SPEED", 1.0)
    eff_speed = round(float(raw_speed), 2)

    # 4. Custom title, chunk chars, verify
    custom_title = (req.get("custom_title") or req.get("title") or "").strip() or None
    raw_chunk = req.get("chunk_chars") or req.get("tts_chunk_chars") or getattr(cfg, "TTS_CHUNK_DEFAULT_CHARS", 500)
    chunk_chars = int(raw_chunk)
    verify = bool(req.get("verify") if req.get("verify") is not None else req.get("verify_final_script", False))

    # 5. Resolve AI Provider Chain
    req_provider = req.get("ai_provider")
    usr_chain = usr.get("ai_provider_chain_json")
    user_models_map = usr.get("ai_models_by_provider_json") or {}

    # Determine base chain from user stored preferences or server defaults
    base_chain: list[str] = []
    if usr_chain and isinstance(usr_chain, list) and len(usr_chain) > 0:
        base_chain = [str(p).lower().strip() for p in usr_chain if p]
    else:
        base_chain = get_server_default_chain(cfg)

    raw_providers: list[str] = []
    if req_provider:
        primary_p = str(req_provider).lower().strip()
        if primary_p == "literal":
            raw_providers = ["literal"]
        else:
            disable_fo = bool(req.get("disable_failover") or req.get("single_provider_only"))
            raw_providers = [primary_p]
            if not disable_fo:
                for p in base_chain:
                    if p and p != primary_p and p != "literal" and p not in raw_providers:
                        raw_providers.append(p)
    else:
        raw_providers = list(base_chain)

    # Validate provider slots: unique, max 3, no literal in fallback, must be registered
    cleaned_providers: list[str] = []
    for p in raw_providers[:3]:
        if p and p not in cleaned_providers:
            desc = get_descriptor(p)
            if desc:
                if p == "literal":
                    if not cleaned_providers:
                        cleaned_providers = ["literal"]
                    break
                cleaned_providers.append(p)

    if not cleaned_providers:
        if mode_cand and mode_cand != RequestMode.LITERAL.value:
            raise AIProviderError(
                "No valid configured AI providers found in chain",
                provider="none",
                safe_detail="No valid AI provider available in chain",
            )
        cleaned_providers = ["literal"]

    # If mode was not explicitly requested or set in user preferences,
    # resolve default mode from the resolved provider chain.
    if mode_cand is None:
        if cleaned_providers and cleaned_providers[0] != "literal":
            mode_cand = RequestMode.STANDARD.value
        else:
            mode_cand = getattr(cfg, "get_default_mode", lambda: "standard")()

    # If primary candidate is literal and mode was not explicitly requested, mode resolves to literal (Item 27)
    if cleaned_providers[0] == "literal" and req_mode_raw is None:
        mode_cand = RequestMode.LITERAL.value

    # Literal mode has strict zero-AI short circuit
    if mode_cand == RequestMode.LITERAL.value:
        return ResolvedJobSettings(
            mode=mode_cand,
            research_depth=None,
            voice=eff_voice,
            speed=eff_speed,
            custom_title=custom_title,
            chunk_chars=chunk_chars,
            verify=False,
            ai_candidates=[AIProviderCandidate(provider_id="literal", model_id="none")],
            research_provider=None,
            research_model=None,
        )

    # Now resolve model for EACH candidate provider in the chain
    candidates: list[AIProviderCandidate] = []
    for prov_id in cleaned_providers:
        if prov_id == "literal":
            candidates.append(AIProviderCandidate(provider_id="literal", model_id="none"))
            continue

        chosen_model: str | None = None
        if prov_id == cleaned_providers[0] and req.get("ai_model"):
            cand_model = str(req["ai_model"]).strip()
            # Validate explicit model through catalog (Item 24)
            if not validate_model_for_provider(prov_id, cand_model):
                raise AIProviderError(
                    f"Model '{cand_model}' is not valid for provider '{prov_id}'",
                    provider=prov_id,
                    model=cand_model,
                    safe_detail=f"Invalid model requested for {prov_id}",
                )
            chosen_model = cand_model
        elif prov_id in user_models_map and user_models_map[prov_id]:
            rem_model = str(user_models_map[prov_id]).strip()
            if validate_model_for_provider(prov_id, rem_model):
                chosen_model = rem_model
            else:
                chosen_model = get_default_model(prov_id)
        else:
            chosen_model = get_default_model(prov_id)

        if not chosen_model:
            chosen_model = get_default_model(prov_id)

        candidates.append(AIProviderCandidate(provider_id=prov_id, model_id=chosen_model))

    # Resolve and snapshot Research identity (Item 13)
    res_provider: str | None = None
    res_model: str | None = None
    if mode_cand == RequestMode.RESEARCH.value:
        for c in candidates:
            desc = get_descriptor(c.provider_id)
            if desc and getattr(desc.capabilities, "research_grounding", False):
                res_provider = c.provider_id
                if c.provider_id == "gemini":
                    res_model = getattr(cfg, "GEMINI_RESEARCH_MODEL", "gemini-3.6-flash")
                else:
                    res_model = c.model_id
                break

    return ResolvedJobSettings(
        mode=mode_cand,
        research_depth=research_depth,
        voice=eff_voice,
        speed=eff_speed,
        custom_title=custom_title,
        chunk_chars=chunk_chars,
        verify=verify,
        ai_candidates=candidates,
        research_provider=res_provider,
        research_model=res_model,
    )
