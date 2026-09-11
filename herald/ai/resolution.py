"""
Central Settings Resolution Service for Herald AI Architecture.
Computes authoritative, typed ResolvedJobSettings with strict precedence:
explicit request override > user preference > server/application default.
"""

from dataclasses import asdict, dataclass, field
from typing import Any

from herald.ai.catalog import validate_model_for_provider
from herald.ai.registry import (
    get_default_model,
    get_descriptor,
    is_provider_configured,
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
        }


def get_server_default_chain(cfg: Any = None) -> list[str]:
    """
    Resolve the server default ordered provider chain:
    AI_PROVIDER -> AI_SECONDARY_PROVIDER -> AI_TERTIARY_PROVIDER
    Validates uniqueness and eliminates unconfigured slots.
    """
    conf = cfg or global_settings
    primary = (getattr(conf, "AI_PROVIDER", "gemini") or "gemini").lower().strip()
    secondary = getattr(conf, "AI_SECONDARY_PROVIDER", None)
    tertiary = getattr(conf, "AI_TERTIARY_PROVIDER", None)

    is_valid, _ = validate_server_default_chain(primary, secondary, tertiary)
    if not is_valid:
        # Fallback strictly to primary
        return [primary]

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
    # Request explicit mode > user default_mode > server default_mode
    req_mode_raw = req.get("mode") or req.get("request_mode")
    usr_mode_raw = usr.get("default_mode")
    cfg_mode_raw = getattr(cfg, "get_default_mode", lambda: "standard")()

    mode_cand = (req_mode_raw or usr_mode_raw or cfg_mode_raw or "standard").lower().strip()
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
        )

    # Provider Chain Resolution:
    # Explicit request provider override > user stored chain > server default chain
    req_provider = req.get("ai_provider")
    usr_chain = usr.get("ai_provider_chain_json")
    user_models_map = usr.get("ai_models_by_provider_json") or {}

    raw_providers: list[str] = []
    if req_provider:
        # Explicit primary override from request
        raw_providers = [str(req_provider).lower().strip()]
    elif usr_chain and isinstance(usr_chain, list) and len(usr_chain) > 0:
        raw_providers = [str(p).lower().strip() for p in usr_chain if p]
    else:
        # Fallback to server default chain
        raw_providers = get_server_default_chain(cfg)

    # Validate provider slots: unique, max 3, no literal in fallback, must be registered
    cleaned_providers: list[str] = []
    for p in raw_providers[:3]:
        if p and p not in cleaned_providers:
            desc = get_descriptor(p)
            if desc:
                # If literal is selected as primary, chain becomes just ['literal']
                if p == "literal":
                    if not cleaned_providers:
                        cleaned_providers = ["literal"]
                    break
                cleaned_providers.append(p)

    if not cleaned_providers:
        cleaned_providers = ["gemini"]

    # Now resolve model for EACH candidate provider in the chain
    candidates: list[AIProviderCandidate] = []
    for prov_id in cleaned_providers:
        if prov_id == "literal":
            candidates.append(AIProviderCandidate(provider_id="literal", model_id="none"))
            continue

        # Model precedence: explicit request model (if primary) > user remembered model for provider > provider default
        chosen_model: str | None = None
        if prov_id == cleaned_providers[0] and req.get("ai_model"):
            chosen_model = str(req["ai_model"]).strip()
        elif prov_id in user_models_map and user_models_map[prov_id]:
            rem_model = str(user_models_map[prov_id]).strip()
            # User Correction 22: Validate remembered model at job creation
            if validate_model_for_provider(prov_id, rem_model):
                chosen_model = rem_model
            else:
                # Stale remembered model fallback to provider approved default
                chosen_model = get_default_model(prov_id)
        else:
            chosen_model = get_default_model(prov_id)

        if not chosen_model:
            chosen_model = get_default_model(prov_id)

        candidates.append(AIProviderCandidate(provider_id=prov_id, model_id=chosen_model))

    return ResolvedJobSettings(
        mode=mode_cand,
        research_depth=research_depth,
        voice=eff_voice,
        speed=eff_speed,
        custom_title=custom_title,
        chunk_chars=chunk_chars,
        verify=verify,
        ai_candidates=candidates,
    )
