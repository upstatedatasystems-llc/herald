"""
Legacy compatibility delegates for herald.ai.
DEPRECATED: Active jobs must not rely on these no-context global provider delegates.
Instead, execution must proceed through execute_with_failover() deriving from the
job's snapshotted provider chain.
"""

from typing import Any

from herald.ai.base import AIProvider
from herald.ai.factory import create_provider, get_ai_provider
from herald.config import settings


def get_research_provider(provider_name: str | None = None, model: str | None = None) -> AIProvider | None:
    """
    Return a research provider capable of Google Search Grounding for legacy callers.
    """
    r_prov = (provider_name or getattr(settings, "RESEARCH_PROVIDER", "gemini") or "gemini").lower().strip()
    if r_prov in ("", "none", "literal"):
        return None

    prov = create_provider(r_prov, model_id=model)
    if prov and prov.capabilities.research_grounding:
        return prov
    return None


def resolve_legacy_job_identity(job: Any) -> tuple[str | None, str | None]:
    """
    Isolated legacy fallback for historical jobs lacking modern ai_provider / ai_model fields.
    Inspects legacy gemini_model column only if modern fields are absent.
    """
    legacy_model = getattr(job, "gemini_model", None)
    if legacy_model:
        return "gemini", str(legacy_model)
    return None, None


def generate_grounded_research(*args, **kwargs):
    p = get_research_provider()
    if p is None:
        raise RuntimeError("No research provider configured")
    return p.generate_grounded_research(*args, **kwargs)


def normalize_research_dossier(*args, **kwargs):
    p = get_research_provider()
    if p is None:
        raise RuntimeError("No research provider configured")
    return p.normalize_research_dossier(*args, **kwargs)


def audit_research_script(*args, **kwargs):
    p = get_research_provider()
    if p is None:
        raise RuntimeError("No research provider configured")
    return p.audit_research_script(*args, **kwargs)


def repair_research_script(*args, **kwargs):
    p = get_research_provider()
    if p is None:
        raise RuntimeError("No research provider configured")
    return p.repair_research_script(*args, **kwargs)


def audit_script_fidelity(*args, **kwargs):
    p = get_ai_provider()
    if p is None:
        raise RuntimeError("No AI provider configured")
    return p.audit_script_fidelity(*args, **kwargs)


def repair_script_fidelity(*args, **kwargs):
    p = get_ai_provider()
    if p is None:
        raise RuntimeError("No AI provider configured")
    return p.repair_script_fidelity(*args, **kwargs)


def generate_podcast_script(*args, **kwargs):
    p = get_ai_provider()
    if p is None:
        raise RuntimeError("No AI provider configured")
    return p.generate_script(*args, **kwargs)
