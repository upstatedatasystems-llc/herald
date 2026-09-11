"""
Legacy compatibility delegates for herald.ai.
DEPRECATED: Active jobs must not rely on these no-context global provider delegates.
Instead, execution must proceed through execute_with_failover() deriving from the
job's snapshotted provider chain.
"""

from herald.ai.factory import get_ai_provider, get_research_provider


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
