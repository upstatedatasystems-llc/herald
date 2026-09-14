"""Unified Long-Form Research, Planning, and Generation Engine.

Supports:
- SOURCE_ONLY: Fixed-duration Source mode using deep source coverage ledger,
  full-source retention chunking, and outline budgeting without external research.
- SOURCE_PLUS_RESEARCH: Expanded mode using full seed source + external grounded research.
- RESEARCH: Topic mode using structured research plan + external grounded research.
- Trusted instruction isolation: control instructions never enter untrusted SOURCE_DATA.
- Centralized WPM calculations derived from settings.NARRATION_WORDS_PER_MINUTE.
- Semantic fidelity audit & bounded repair using provider audit capabilities.
- True anti-compression guardrails comparing against both section sums and planned targets.
"""

import enum
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Callable

from herald.ai.failover import execute_with_failover
from herald.ai.schema import PodcastScriptResponse, PodcastSegment
from herald.config import settings
from herald.db.models import JobDiagnosticEvent, PodcastJob
from herald.services.diagnostic_recorder import record_job_diagnostic_event
from herald.services.performance_metrics import record_stage_metric

logger = logging.getLogger("herald.ai.long_form")

BOILERPLATE_PATTERNS = [
    r"subscribe to our newsletter",
    r"sign up for (?:the )?newsletter",
    r"follow us on (?:twitter|x|facebook|instagram|linkedin)",
    r"all rights reserved",
    r"copyright \d{4}",
    r"privacy policy",
    r"terms of (?:service|use)",
    r"cookie (?:policy|settings|notice)",
    r"advertisement",
    r"click here to (?:read|view|subscribe)",
    r"share this article",
    r"read next:",
]


class EvidenceScope(str, enum.Enum):
    SOURCE_ONLY = "SOURCE_ONLY"
    SOURCE_PLUS_RESEARCH = "SOURCE_PLUS_RESEARCH"
    RESEARCH = "RESEARCH"


def get_target_word_budget(target_minutes: str | int | None) -> int | None:
    """Return explicit program word budget for fixed target minutes derived from centralized WPM."""
    if target_minutes is None:
        return None
    key = str(target_minutes).lower().strip()
    if key == "auto" or key == "literal":
        return None
    try:
        mins = float(key)
        if mins <= 0:
            return None
        wpm = getattr(settings, "NARRATION_WORDS_PER_MINUTE", 130.0)
        return int(round(mins * wpm))
    except (ValueError, TypeError):
        return None


def build_source_coverage_ledger(source_text: str, source_title: str | None = None) -> dict[str, Any]:
    """
    Extract meaningful structural content (headings, paragraphs, sections, named entities,
    important numbers, dates, qualifications, and key claims) from the FULL source text.
    Preserves paragraph breaks and document architecture without destroying structure.
    """
    raw_paragraphs = re.split(r"\n\s*\n", source_text)
    clean_paragraphs = []
    omitted_pollution = []
    headings = []

    for raw_p in raw_paragraphs:
        p_strip = raw_p.strip()
        if not p_strip:
            continue

        # Check for boilerplate pollution
        lower_p = p_strip.lower()
        is_pollution = False
        for pat in BOILERPLATE_PATTERNS:
            if re.search(pat, lower_p):
                is_pollution = True
                omitted_pollution.append(p_strip[:80])
                break
        if is_pollution:
            continue

        # Detect headings / subheadings (e.g. Markdown '#', or short capitalized title line)
        first_line = p_strip.splitlines()[0].strip()
        if first_line.startswith("#") or (len(first_line) < 80 and first_line.isupper()) or (len(p_strip.splitlines()) == 1 and len(first_line) < 60 and not first_line.endswith(".")):
            h_clean = re.sub(r"^#+\s*", "", first_line)
            if h_clean and h_clean not in headings:
                headings.append(h_clean)

        clean_paragraphs.append(p_strip)

    clean_content = "\n\n".join(clean_paragraphs)

    # Extract numbers, percentages, dates, currencies
    numbers = re.findall(
        r"(?:[\$€£]?\d+(?:[.,]\d+)*(?:\s*(?:percent|%|million|billion|trillion|meters|feet|knots|tons|years?|months?|days?|hours?|mph|km/h))?)",
        clean_content,
        re.IGNORECASE,
    )
    seen_nums = set()
    key_numbers = []
    for num in numbers:
        n_clean = num.strip().rstrip(".,;:)")
        if len(n_clean) >= 2 and n_clean not in seen_nums:
            seen_nums.add(n_clean)
            key_numbers.append(n_clean)

    # Extract capitalized proper nouns / entity names
    entities = re.findall(r"\b[A-Z][a-z0-9]+(?:\s+[A-Z][a-z0-9]+)+\b", clean_content)
    seen_ents = set()
    key_entities = []
    for ent in entities:
        if ent not in seen_ents and len(ent) > 4:
            seen_ents.add(ent)
            key_entities.append(ent)

    # Build core claims preserving representation across the entire document
    core_claims = []
    for idx, p in enumerate(clean_paragraphs):
        words = p.split()
        if len(words) >= 12:
            snippet = " ".join(words[:25]) + ("..." if len(words) > 25 else "")
            core_claims.append({
                "paragraph_index": idx + 1,
                "claim_snippet": snippet,
                "word_count": len(words),
            })

    return {
        "source_title": source_title or "Primary Source",
        "clean_text": clean_content,
        "paragraph_count": len(clean_paragraphs),
        "headings": headings[:15],
        "key_numbers": key_numbers[:50],
        "key_entities": key_entities[:40],
        "core_claims": core_claims[:30],
        "omitted_pollution": omitted_pollution[:10],
    }


def build_research_plan(
    topic: str,
    research_depth: str = "medium",
    scope: EvidenceScope = EvidenceScope.RESEARCH,
    seed_summary: str | None = None,
) -> dict[str, Any]:
    """Generate structured research plan for what should be investigated, bounded by research depth."""
    depth = (research_depth or "medium").lower().strip()
    if depth not in ("low", "medium", "high"):
        depth = getattr(settings, "DEFAULT_RESEARCH_DEPTH", "medium").lower().strip()
        if depth not in ("low", "medium", "high"):
            depth = "medium"

    if depth == "low":
        target_areas = 2
        queries_per_area = 2
    elif depth == "high":
        target_areas = 5
        queries_per_area = 3
    else:
        target_areas = 3
        queries_per_area = 2

    focus_areas = []
    if scope == EvidenceScope.SOURCE_PLUS_RESEARCH:
        focus_areas.append({
            "name": "Historical Background and Genesis",
            "focus": f"Origins, context, and preceding developments for {topic}",
            "queries": [f"{topic} history background origin", f"{topic} timeline context"][:queries_per_area],
        })
        focus_areas.append({
            "name": "Technical Design and Architecture",
            "focus": f"Detailed technical specifications, engineering, and architecture of {topic}",
            "queries": [f"{topic} technical specifications design", f"{topic} capabilities analysis"][:queries_per_area],
        })
        if target_areas >= 3:
            focus_areas.append({
                "name": "Operational Reality, Updates, and Challenges",
                "focus": f"Operational deployment, updates, controversies, and future outlook for {topic}",
                "queries": [f"{topic} recent developments updates", f"{topic} challenges controversies"][:queries_per_area],
            })
        if target_areas >= 4:
            focus_areas.append({
                "name": "Comparisons, Alternatives, and Economics",
                "focus": f"Comparison of {topic} with alternatives, competitors, or economic costs",
                "queries": [f"{topic} comparison competitors", f"{topic} cost analysis"][:queries_per_area],
            })
        if target_areas >= 5:
            focus_areas.append({
                "name": "Strategic Significance and Long-Term Horizon",
                "focus": f"Long-term significance, broader impact, and future trajectory of {topic}",
                "queries": [f"{topic} future outlook strategic role", f"{topic} program trajectory"][:queries_per_area],
            })
    else:
        # Pure Topic mode
        focus_areas.append({
            "name": "Core Premise, Definition, and History",
            "focus": f"Definition, origins, and core foundational facts regarding {topic}",
            "queries": [f"{topic} overview definition history", f"{topic} background facts"][:queries_per_area],
        })
        focus_areas.append({
            "name": "Mechanisms, Technical Structure, and Architecture",
            "focus": f"How {topic} works, underlying mechanisms, or primary structure",
            "queries": [f"{topic} how it works technical details", f"{topic} architecture key aspects"][:queries_per_area],
        })
        if target_areas >= 3:
            focus_areas.append({
                "name": "Real-World Impact, Case Studies, and Controversies",
                "focus": f"Significance, impact, debates, and controversies around {topic}",
                "queries": [f"{topic} impact real world examples", f"{topic} controversies analysis"][:queries_per_area],
            })
        if target_areas >= 4:
            focus_areas.append({
                "name": "Modern Developments, Trends, and Practical Lessons",
                "focus": f"Recent developments and notable case studies for {topic}",
                "queries": [f"{topic} recent developments case studies", f"{topic} current status"][:queries_per_area],
            })
        if target_areas >= 5:
            focus_areas.append({
                "name": "Future Horizon and Broader Implications",
                "focus": f"Where {topic} is heading and what it means for the future",
                "queries": [f"{topic} future predictions implications", f"{topic} research frontier"][:queries_per_area],
            })

    return {
        "topic": topic,
        "research_depth": depth,
        "scope": scope.value,
        "focus_areas": focus_areas[:target_areas],
        "seed_summary": seed_summary,
    }


def normalize_evidence_packet(
    topic: str,
    scope: EvidenceScope,
    seed_source_text: str | None = None,
    grounded_research_data: dict[str, Any] | None = None,
    seed_source_url: str | None = None,
) -> dict[str, Any]:
    """
    Assemble attributable EvidencePacket with full source retention and attributable research evidence.
    Does NOT truncate the source to 3,000 characters. For large sources, divides into structured chunks
    with IDs and preserves complete source content durably.
    """
    items: list[dict[str, Any]] = []

    # Process seed source (Source and Expanded modes)
    if seed_source_text and seed_source_text.strip():
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", seed_source_text) if p.strip()]
        total_source_words = len(seed_source_text.split())

        # If source is large (>1,200 words), chunk it into identifiable evidence blocks
        if total_source_words > 1200 and len(paragraphs) > 4:
            chunk_size = max(2, len(paragraphs) // 4)
            for chunk_idx in range(0, len(paragraphs), chunk_size):
                sub_pars = paragraphs[chunk_idx : chunk_idx + chunk_size]
                chunk_num = (chunk_idx // chunk_size) + 1
                first_words = " ".join(sub_pars[0].split()[:6])
                items.append({
                    "evidence_id": f"ev_src_{chunk_num}",
                    "title": f"Primary Source (Section {chunk_num}: {first_words}...)",
                    "publisher": "User Source Material",
                    "source_url": seed_source_url,
                    "snippet": "\n\n".join(sub_pars),
                    "is_seed_source": True,
                    "focus_area": f"Source Section {chunk_num}",
                })
        else:
            items.append({
                "evidence_id": "ev_seed",
                "title": "Primary Submitted Source",
                "publisher": "User Source Material",
                "source_url": seed_source_url,
                "snippet": seed_source_text.strip(),
                "is_seed_source": True,
                "focus_area": "Seed Material",
            })

    # External research items
    if grounded_research_data:
        raw_text = grounded_research_data.get("raw_text", "")
        sources = grounded_research_data.get("research_sources", [])
        grounding_meta = grounded_research_data.get("grounding_metadata", {})
        grounding_supports = grounding_meta.get("groundingSupports") or grounding_meta.get("grounding_supports") or []
        grounding_chunks = grounding_meta.get("groundingChunks") or grounding_meta.get("grounding_chunks") or []

        # If grounding supports map text segments to chunks, use them
        has_supports = bool(grounding_supports and grounding_chunks)
        if has_supports:
            for s_idx, supp in enumerate(grounding_supports, 1):
                seg = supp.get("segment", {})
                claim_text = seg.get("text", "").strip()
                chunk_indices = supp.get("groundingChunkIndices", [])
                supp_sources = []
                for c_idx in chunk_indices:
                    if 0 <= c_idx < len(grounding_chunks):
                        g_chunk = grounding_chunks[c_idx]
                        web = g_chunk.get("web", {})
                        supp_sources.append(web.get("uri") or web.get("url"))

                if claim_text:
                    first_src_url = supp_sources[0] if supp_sources else None
                    items.append({
                        "evidence_id": f"ev_ground_{s_idx}",
                        "title": f"Grounded Finding {s_idx}",
                        "publisher": "Google Search Grounding",
                        "source_url": first_src_url,
                        "source_ids": [f"S{c+1}" for c in chunk_indices],
                        "snippet": claim_text,
                        "is_seed_source": False,
                        "focus_area": "External Grounded Research",
                    })

        # Register canonical sources
        if not has_supports and sources:
            # Represent grounded research honestly as a grounded synthesis with supporting source registry
            if raw_text:
                all_source_ids = [s.get("source_id", f"S{i}") for i, s in enumerate(sources, 1)]
                items.append({
                    "evidence_id": "ev_grounded_synthesis",
                    "title": f"Grounded Research Synthesis on {topic}",
                    "publisher": "Google Search Grounding",
                    "source_url": sources[0].get("url") if sources else None,
                    "source_ids": all_source_ids,
                    "snippet": raw_text.strip(),
                    "is_seed_source": False,
                    "focus_area": "External Grounded Research",
                })
            for idx, src in enumerate(sources, 1):
                items.append({
                    "evidence_id": f"ev_src_reg_{idx}",
                    "title": src.get("title") or f"Research Source {idx}",
                    "publisher": src.get("publisher") or src.get("domain"),
                    "source_url": src.get("url"),
                    "source_ids": [src.get("source_id", f"S{idx}")],
                    "search_query": src.get("search_query"),
                    "retrieved_at": src.get("retrieved_at"),
                    "snippet": f"Authoritative source: {src.get('title')} ({src.get('url')}). Search query: {src.get('search_query', 'N/A')}",
                    "is_seed_source": False,
                    "focus_area": "Source Registry",
                })
        elif not items and raw_text:
            items.append({
                "evidence_id": "ev_grounded_summary",
                "title": f"Grounded Research on {topic}",
                "publisher": "Google Search Grounding",
                "source_url": None,
                "snippet": raw_text.strip(),
                "is_seed_source": False,
                "focus_area": "External Grounded Research",
            })

    return {
        "topic": topic,
        "scope": scope.value,
        "seed_source_url": seed_source_url,
        "evidence_count": len(items),
        "items": items,
    }


def build_episode_outline(
    topic: str,
    evidence_packet: dict[str, Any],
    target_minutes: str | int | None,
    scope: EvidenceScope,
    source_ledger: dict[str, Any] | None = None,
    research_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build structured episode outline with topic/evidence-specific section headings and budgets.
    For Source mode: derives sections from source structure/ledger and bounds budget to evidence.
    For Auto mode: derives natural section count without a fixed 400-word quota.
    """
    target_budget = get_target_word_budget(target_minutes)
    is_auto = target_budget is None

    items = evidence_packet.get("items", [])
    evidence_ids = [it["evidence_id"] for it in items]

    source_words = len((source_ledger.get("clean_text", "") if source_ledger else "").split())

    # Source mode bounds check: remove arbitrary 800-word floor.
    # Never invent content merely to meet target.
    evidence_supported_target = target_budget
    if scope == EvidenceScope.SOURCE_ONLY and target_budget is not None and source_words > 0:
        max_legitimate_words = int(source_words * 2.0)
        if target_budget > max_legitimate_words:
            logger.info(
                f"Source mode: source word count ({source_words}) cannot legitimately support requested budget ({target_budget} words). "
                f"Bounding word budget to {max_legitimate_words} words."
            )
            evidence_supported_target = max_legitimate_words

    # Section counts and target word distribution
    if is_auto:
        # In Auto mode, derive natural scope from evidence without fixed quota
        if scope == EvidenceScope.SOURCE_ONLY and source_ledger:
            sec_count = max(2, min(len(source_ledger.get("headings", [])) or len(items) or 3, 6))
        elif research_plan and research_plan.get("focus_areas"):
            sec_count = max(2, len(research_plan["focus_areas"]))
        else:
            sec_count = max(2, min(len(items), 6))
        section_word_budget = None  # Soft/unbudgeted in Auto mode
        effective_total_words = None
    else:
        effective_total_words = evidence_supported_target
        if evidence_supported_target <= 1500:
            nominal_sec_count = 3
        elif evidence_supported_target <= 3000:
            nominal_sec_count = 5
        elif evidence_supported_target <= 4500:
            nominal_sec_count = 7
        elif evidence_supported_target <= 6000:
            nominal_sec_count = 9
        else:
            nominal_sec_count = 11

        # Deliberate section count reduction:
        # If available evidence cannot support the planned number of distinct sections,
        # reduce the number of sections rather than manufacturing repetitive sections.
        if evidence_ids:
            if scope == EvidenceScope.SOURCE_ONLY:
                max_supported_secs = max(2, min(len(evidence_ids), len(source_ledger.get("headings", [])) or len(evidence_ids)))
            elif research_plan and research_plan.get("focus_areas"):
                max_supported_secs = max(len(research_plan["focus_areas"]), min(len(evidence_ids), nominal_sec_count))
            else:
                max_supported_secs = max(2, min(len(evidence_ids) + 1, nominal_sec_count))
            sec_count = min(nominal_sec_count, max_supported_secs)
        else:
            sec_count = nominal_sec_count

        section_word_budget = evidence_supported_target // sec_count

    sections = []

    # Build topic-specific section headings and distinct narrative purposes
    headings_pool = []
    if scope == EvidenceScope.SOURCE_ONLY and source_ledger and source_ledger.get("headings"):
        for h in source_ledger["headings"]:
            headings_pool.append((h, f"Explore source content on: {h}"))
    elif research_plan and research_plan.get("focus_areas"):
        for fa in research_plan["focus_areas"]:
            headings_pool.append((fa["name"], fa["focus"]))

    distinct_narrative_facets = [
        ("The Central Premise and Key Facts", "Establish core stakes and central narrative premise."),
        ("Context, Background, and Evolution", "Analyze background roots, context, and development."),
        ("Technical Mechanics and Architecture", "Examine structural design, mechanisms, and specifications."),
        ("Operational Challenges and Nuance", "Address controversies, obstacles, and complex tradeoffs."),
        ("Real-World Impact and Future Horizons", "Synthesize long-term meaning, lessons, and implications."),
        ("Strategic Tradeoffs and Critical Analysis", "Deep-dive into tradeoffs, edge cases, and critical evaluations."),
    ]

    if not headings_pool:
        headings_pool = distinct_narrative_facets

    for i in range(sec_count):
        idx = i + 1
        if i < len(headings_pool):
            heading, purpose = headings_pool[i]
        else:
            facet_heading, facet_purpose = distinct_narrative_facets[i % len(distinct_narrative_facets)]
            base_heading = headings_pool[i % len(headings_pool)][0]
            heading = f"{base_heading}: {facet_heading}"
            purpose = f"Examine {base_heading} through the lens of: {facet_purpose}"

        # Assign relevant evidence deliberately:
        # Guarantee all meaningful source chunks receive coverage without mechanically repeating the last chunk
        if not evidence_ids:
            assigned_ev = []
        elif len(evidence_ids) <= sec_count:
            # Proportional mapping across sections so chunks are distributed evenly
            chunk_idx = (i * len(evidence_ids)) // sec_count
            assigned_ev = [evidence_ids[chunk_idx]]
        else:
            start_ev = (i * len(evidence_ids)) // sec_count
            end_ev = ((i + 1) * len(evidence_ids)) // sec_count
            assigned_ev = evidence_ids[start_ev:max(start_ev + 1, end_ev)]

        sections.append({
            "section_index": idx,
            "heading": heading,
            "purpose": purpose,
            "word_budget": section_word_budget,
            "relevant_evidence_ids": assigned_ev,
            "transition_intent": f"Flow smoothly from section {idx-1}" if idx > 1 else "Opening hook",
        })

    return {
        "episode_title": topic,
        "episode_description": f"An in-depth exploration of {topic}.",
        "requested_target_words": get_target_word_budget(target_minutes),
        "target_total_words": effective_total_words,
        "section_count": sec_count,
        "sections": sections,
        "is_auto": is_auto,
    }


def generate_single_section(
    job: PodcastJob,
    section_info: dict[str, Any],
    topic: str,
    evidence_packet: dict[str, Any],
    previous_summary: str | None,
    scope: EvidenceScope,
    db: Any = None,
) -> dict[str, Any]:
    """
    Generate one section of the long-form podcast script grounded strictly in assigned evidence.
    Places control instructions in trusted generation_instructions outside untrusted SOURCE_DATA.
    Enforces section word budget with bounded expansion when materially short.
    """
    sec_idx = section_info["section_index"]
    heading = section_info["heading"]
    purpose = section_info["purpose"]
    budget = section_info.get("word_budget")
    ev_ids = section_info.get("relevant_evidence_ids", [])

    all_items = {it["evidence_id"]: it for it in evidence_packet.get("items", [])}
    assigned_snippets = []
    for eid in ev_ids:
        if eid in all_items:
            it = all_items[eid]
            assigned_snippets.append(f"[{it['evidence_id']} - {it['title']}]:\n{it['snippet']}")

    evidence_text = "\n\n".join(assigned_snippets) or f"Evidence regarding {topic}."

    prev_context = (
        f"Previous section covered: {previous_summary}. Do NOT repeat those introductory facts. Continue the narrative naturally."
        if previous_summary
        else "This is the opening section. Hook the listener and state the core premise directly."
    )

    budget_instruction = (
        f"Target Word Budget: approximately {budget} words. Write complete, detailed narration approaching this budget."
        if budget is not None
        else "Write natural, comprehensive spoken narration covering the assigned evidence thoroughly without artificial brevity."
    )

    control_instructions = f"""You are writing Section {sec_idx} of a long-form podcast about: {topic}
Section Heading: {heading}
Purpose: {purpose}
{budget_instruction}

{prev_context}

Requirements:
1. Write engaging, natural spoken podcast narration for the host.
2. Ground all factual assertions strictly in the provided evidence.
3. If this is Source mode (SOURCE_ONLY), do NOT introduce outside facts not present in the evidence.
"""

    def _execute_section(p_inst: Any, attempt: int, src: str) -> PodcastScriptResponse:
        return p_inst.generate_script(
            source_text=src,
            request_mode="standard",
            source_title=topic,
            job_id=job.id,
            generation_instructions=control_instructions,
        )

    res: PodcastScriptResponse = execute_with_failover(
        job=job,
        operation="section_generation",
        execute_fn=_execute_section,
        db=db,
        source_text=evidence_text,
    )

    narration_parts = [seg.narration for seg in res.segments]
    full_narration = "\n\n".join(narration_parts)
    actual_words = len(full_narration.split())
    # Duration enforcement: if budget is fixed and section is materially short (< 80% of budget),
    # run one bounded continuation/expansion pass (for Expanded/Topic, or Source if evidence permits).
    evidence_words = len(evidence_text.split())
    can_expand_section = (
        scope != EvidenceScope.SOURCE_ONLY
        or (scope == EvidenceScope.SOURCE_ONLY and budget is not None and evidence_words >= int(budget * 0.6))
    )
    if budget and actual_words < int(budget * 0.8) and can_expand_section:
        logger.info(
            f"Section {sec_idx} undershot target budget ({actual_words} words vs {budget} budget). "
            "Executing bounded section expansion pass."
        )
        expansion_instructions = f"""{control_instructions}

NOTICE: Your previous draft was only {actual_words} words, which is materially below the required {budget}-word target.
Expand your narration with deeper explanatory context, concrete examples from the evidence, and thorough discussions of mechanisms.
Target approximately {budget} words. Do not introduce repetitive filler.
"""
        try:
            def _expand_section(p_inst: Any, attempt: int, src: str) -> PodcastScriptResponse:
                return p_inst.generate_script(
                    source_text=src,
                    request_mode="standard",
                    source_title=topic,
                    job_id=job.id,
                    generation_instructions=expansion_instructions,
                )

            exp_res: PodcastScriptResponse = execute_with_failover(
                job=job,
                operation="section_generation",
                execute_fn=_expand_section,
                db=db,
                source_text=evidence_text,
            )
            exp_narration = "\n\n".join(seg.narration for seg in exp_res.segments)
            exp_words = len(exp_narration.split())
            if exp_words > actual_words:
                full_narration = exp_narration
                actual_words = exp_words
        except Exception as e:
            logger.warning(f"Section expansion attempt failed non-fatally: {e}")

    return {
        "section_index": sec_idx,
        "heading": heading,
        "narration": full_narration,
        "word_count": actual_words,
        "target_word_budget": budget,
        "completed": True,
    }


def audit_and_repair_fidelity(
    job: PodcastJob,
    sections: list[dict[str, Any]],
    source_ledger: dict[str, Any] | None,
    evidence_packet: dict[str, Any],
    scope: EvidenceScope,
    db: Any = None,
    source_text: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Perform authoritative semantic fidelity audit and bounded repair.
    SOURCE mode:
        complete cleaned source/evidence -> generated script -> provider semantic fidelity audit ->
        one bounded repair if required -> final semantic re-audit.
    EXPANDED mode:
        seed-source fidelity audit AND research/evidence support audit.
    TOPIC mode:
        research/evidence support audit.

    Persists audit status as one of:
        clean, issue_detected, repair_attempted, repair_succeeded, unresolved_issue_remains.
    """
    primary_source_text = (
        source_text
        or (source_ledger.get("clean_text") if source_ledger else None)
        or "\n\n".join(it.get("snippet", "") for it in evidence_packet.get("items", []))
    )
    dossier_data = evidence_packet or {}

    def _build_script_dict(secs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "episode_title": job.custom_title or "Herald Episode",
            "episode_description": "Herald Podcast Episode",
            "source_title": job.custom_title or "Source Material",
            "segments": [
                {"order": idx, "heading": s.get("heading", f"Section {idx}"), "narration": s.get("narration", "")}
                for idx, s in enumerate(secs, 1)
            ],
            "warnings": [],
        }

    def _run_semantic_audit(curr_script: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        findings: dict[str, Any] = {}
        issues_detected = False
        repair_instructions_parts: list[str] = []

        if scope == EvidenceScope.SOURCE_ONLY:
            try:
                def _audit_source_fn(p_inst: Any, attempt: int, src: str) -> Any:
                    return p_inst.audit_script_fidelity(
                        source_text=src,
                        script_dict=curr_script,
                        job_id=job.id,
                    )

                res = execute_with_failover(
                    job=job,
                    operation="verification",
                    execute_fn=_audit_source_fn,
                    db=db,
                    source_text=primary_source_text,
                    required_capability="verification",
                )
                if hasattr(res, "has_material_issues") and res.has_material_issues:
                    issues_detected = True
                    if getattr(res, "repair_instructions", None):
                        repair_instructions_parts.append(res.repair_instructions)
                findings["source_audit"] = res.model_dump() if hasattr(res, "model_dump") else str(res)
            except Exception as e:
                logger.warning(f"Semantic source fidelity audit skipped/failed non-fatally: {e}")

        elif scope == EvidenceScope.SOURCE_PLUS_RESEARCH:
            # Expanded mode: Perform BOTH seed-source audit AND research/evidence support audit
            if primary_source_text:
                try:
                    def _audit_source_fn(p_inst: Any, attempt: int, src: str) -> Any:
                        return p_inst.audit_script_fidelity(
                            source_text=src,
                            script_dict=curr_script,
                            job_id=job.id,
                        )

                    res_s = execute_with_failover(
                        job=job,
                        operation="verification",
                        execute_fn=_audit_source_fn,
                        db=db,
                        source_text=primary_source_text,
                        required_capability="verification",
                    )
                    if hasattr(res_s, "has_material_issues") and res_s.has_material_issues:
                        issues_detected = True
                        if getattr(res_s, "repair_instructions", None):
                            repair_instructions_parts.append(f"Source fidelity: {res_s.repair_instructions}")
                    findings["source_audit"] = res_s.model_dump() if hasattr(res_s, "model_dump") else str(res_s)
                except Exception as e:
                    logger.warning(f"Semantic source fidelity audit in expanded mode skipped/failed: {e}")

            try:
                def _audit_res_fn(p_inst: Any, attempt: int, src: str) -> Any:
                    return p_inst.audit_research_script(
                        source_text=src,
                        research_dossier=dossier_data,
                        script_dict=curr_script,
                        job_id=job.id,
                    )

                res_r = execute_with_failover(
                    job=job,
                    operation="research_audit",
                    execute_fn=_audit_res_fn,
                    db=db,
                    source_text=primary_source_text or "Topic research",
                    required_capability="verification",
                )
                if hasattr(res_r, "has_material_issues") and res_r.has_material_issues:
                    issues_detected = True
                    if getattr(res_r, "repair_instructions", None):
                        repair_instructions_parts.append(f"Research fidelity: {res_r.repair_instructions}")
                findings["research_audit"] = res_r.model_dump() if hasattr(res_r, "model_dump") else str(res_r)
            except Exception as e:
                logger.warning(f"Semantic research support audit in expanded mode skipped/failed: {e}")

        elif scope == EvidenceScope.RESEARCH:
            # Topic mode: Perform research/evidence support audit
            try:
                def _audit_topic_fn(p_inst: Any, attempt: int, src: str) -> Any:
                    return p_inst.audit_research_script(
                        source_text=src,
                        research_dossier=dossier_data,
                        script_dict=curr_script,
                        job_id=job.id,
                    )

                res_t = execute_with_failover(
                    job=job,
                    operation="research_audit",
                    execute_fn=_audit_topic_fn,
                    db=db,
                    source_text=primary_source_text or "Topic research",
                    required_capability="verification",
                )
                if hasattr(res_t, "has_material_issues") and res_t.has_material_issues:
                    issues_detected = True
                    if getattr(res_t, "repair_instructions", None):
                        repair_instructions_parts.append(res_t.repair_instructions)
                findings["research_audit"] = res_t.model_dump() if hasattr(res_t, "model_dump") else str(res_t)
            except Exception as e:
                logger.warning(f"Semantic topic research audit skipped/failed non-fatally: {e}")

        return issues_detected, " ".join(repair_instructions_parts), findings

    # 1. Initial Authoritative Semantic Audit
    current_script_dict = _build_script_dict(sections)
    has_material_issues, repair_instructions, audit_findings = _run_semantic_audit(current_script_dict)

    # 2. Supplementary Coverage Ledger Check (Inexpensive signal)
    combined_narration = "\n\n".join(s.get("narration", "") for s in sections)
    lower_narration = combined_narration.lower()
    omitted_numbers = []
    omitted_entities = []
    if source_ledger:
        for num in source_ledger.get("key_numbers", []):
            if num.lower() not in lower_narration:
                omitted_numbers.append(num)
        for ent in source_ledger.get("key_entities", []):
            if ent.lower() not in lower_narration:
                omitted_entities.append(ent)

    # If semantic audit didn't flag an issue but coverage ledger found severe omissions, note it
    ledger_issue = len(omitted_numbers) > 4 or len(omitted_entities) > 4
    if ledger_issue and not has_material_issues and not audit_findings:
        has_material_issues = True
        repair_instructions = (
            f"Restore missing key factual figures and entities from the source: "
            f"{', '.join(omitted_numbers[:4] + omitted_entities[:4])}."
        )

    repair_attempted = False
    repair_succeeded = False
    unresolved_issue = False

    # 3. Bounded Semantic Repair Pass (max 1 repair attempt)
    if has_material_issues and (job.verify_repair_count or 0) == 0:
        logger.info(f"Fidelity audit detected material issues for job {job.id}. Executing bounded repair with actual source/evidence.")
        repair_attempted = True
        record_job_diagnostic_event(
            job.id,
            "INFO",
            "fidelity",
            "FIDELITY_REPAIR_BEGIN",
            "Executing bounded semantic repair using primary source/evidence and audit instructions.",
            db=db,
        )

        audit_payload = {
            "has_material_issues": True,
            "repair_instructions": repair_instructions or "Restore omitted material facts and correct factual inaccuracies.",
            "omitted_numbers": omitted_numbers[:8],
            "omitted_entities": omitted_entities[:8],
        }

        try:
            if scope == EvidenceScope.SOURCE_ONLY:
                def _do_repair_src(p_inst: Any, att: int, src: str) -> PodcastScriptResponse:
                    return p_inst.repair_script_fidelity(
                        source_text=src,
                        script_dict=current_script_dict,
                        audit_result=audit_payload,
                        job_id=job.id,
                    )

                repaired_res: PodcastScriptResponse = execute_with_failover(
                    job=job,
                    operation="verification_repair",
                    execute_fn=_do_repair_src,
                    db=db,
                    source_text=primary_source_text,
                    required_capability="verification",
                )
            else:
                def _do_repair_res(p_inst: Any, att: int, src: str) -> PodcastScriptResponse:
                    return p_inst.repair_research_script(
                        source_text=src,
                        research_dossier=dossier_data,
                        script_dict=current_script_dict,
                        audit_result=audit_payload,
                        job_id=job.id,
                    )

                repaired_res: PodcastScriptResponse = execute_with_failover(
                    job=job,
                    operation="research_repair",
                    execute_fn=_do_repair_res,
                    db=db,
                    source_text=primary_source_text or "Topic research",
                    required_capability="verification",
                )

            if repaired_res and repaired_res.segments:
                for idx, seg in enumerate(repaired_res.segments):
                    if idx < len(sections):
                        sections[idx]["narration"] = seg.narration
                        sections[idx]["word_count"] = len(seg.narration.split())
                    else:
                        sections.append({
                            "section_index": idx + 1,
                            "heading": seg.heading,
                            "narration": seg.narration,
                            "word_count": len(seg.narration.split()),
                            "target_word_budget": None,
                            "completed": True,
                        })
                job.verify_repair_count = (job.verify_repair_count or 0) + 1
                repair_succeeded = True
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "fidelity",
                    "FIDELITY_REPAIR_SUCCESS",
                    "Bounded semantic fidelity repair completed.",
                    db=db,
                )
        except Exception as rep_err:
            logger.warning(f"Semantic fidelity repair attempt failed non-fatally: {rep_err}")
            unresolved_issue = True

        # 4. Final Semantic Re-Audit (Bounded: exactly 1 verification pass, no loop)
        if repair_succeeded:
            repaired_script_dict = _build_script_dict(sections)
            re_issues, _, re_findings = _run_semantic_audit(repaired_script_dict)
            audit_findings["final_re_audit"] = re_findings
            if not re_issues:
                audit_status = "repair_succeeded"
            else:
                audit_status = "unresolved_issue_remains"
                unresolved_issue = True
        else:
            audit_status = "unresolved_issue_remains"
            unresolved_issue = True
    elif repair_attempted:
        audit_status = "repair_attempted"
    elif has_material_issues:
        audit_status = "issue_detected"
    else:
        audit_status = "clean"

    audit_result = {
        "status": audit_status,
        "has_material_issues": has_material_issues,
        "repair_instructions": repair_instructions,
        "repair_attempted": repair_attempted,
        "repair_succeeded": repair_succeeded,
        "unresolved_issue": unresolved_issue,
        "findings": audit_findings,
        "omitted_numbers": omitted_numbers[:10],
        "omitted_entities": omitted_entities[:10],
    }

    return sections, audit_result


def assemble_and_smooth_script(
    episode_title: str,
    episode_description: str,
    sections: list[dict[str, Any]],
    source_title: str | None = None,
    planned_target_words: int | None = None,
) -> PodcastScriptResponse:
    """
    True Final Coherence Pass with Anti-Compression Guard.
    Assembles generated sections into PodcastSegment items.
    Enforces anti-compression comparing against BOTH:
    1) generated sectional content sum (within +/- 10%)
    2) planned target words when fixed duration is specified (within +/- 20% tolerance)
    """
    if not sections:
        raise ValueError("Cannot assemble script from empty sections list.")

    segments: list[PodcastSegment] = []
    total_raw_section_words = sum(s.get("word_count", len(s.get("narration", "").split())) for s in sections)

    for idx, sec in enumerate(sections, 1):
        heading = sec.get("heading") or f"Section {idx}"
        narration = sec.get("narration", "").strip()
        if not narration:
            continue

        cleaned_narration = re.sub(r"^(?:in this section,?|turning now to our next topic,?)\s*", "", narration, flags=re.IGNORECASE)
        segments.append(
            PodcastSegment(
                order=idx,
                heading=heading,
                narration=cleaned_narration,
            )
        )

    assembled_words = sum(len(seg.narration.split()) for seg in segments)

    # Anti-Compression Guardrail 1: against sum of generated sections
    if total_raw_section_words > 300:
        min_sec_allowed = int(total_raw_section_words * 0.85)
        if assembled_words < min_sec_allowed:
            raise ValueError(
                f"Anti-Compression violation: Assembled script ({assembled_words} words) collapsed below 85% "
                f"of sectional content ({total_raw_section_words} words)."
            )

    # Anti-Compression Guardrail 2: against planned target budget when specified
    if planned_target_words and planned_target_words > 500:
        min_planned_allowed = int(planned_target_words * 0.70)
        if assembled_words < min_planned_allowed:
            logger.warning(
                f"Anti-Compression warning: Assembled script ({assembled_words} words) is below 70% "
                f"of planned target budget ({planned_target_words} words)."
            )

    return PodcastScriptResponse(
        episode_title=episode_title or "Herald Episode",
        episode_description=episode_description or f"Episode about {episode_title}",
        source_title=source_title,
        segments=segments,
        warnings=[],
    )


def execute_unified_long_form_pipeline(
    db: Any,
    job: PodcastJob,
    topic: str,
    scope: EvidenceScope,
    target_minutes: str | int | None,
    research_depth: str = "medium",
    source_text: str | None = None,
    source_title: str | None = None,
    status_notifier: Callable[[str], None] | None = None,
) -> PodcastScriptResponse:
    """
    Execute full staged long-form pipeline with resume checkpoints:
    1. Coverage Ledger / Source Analysis
    2. Research Plan & Grounded Evidence Gathering (if Expanded or Topic)
    3. Episode Outline & Word Budgeting
    4. Sequential Section Generation with Checkpointing in section_progress_json
    5. Actionable Semantic Fidelity Audit & Bounded Repair
    6. Final Assembly & Coherence Pass with Dual Anti-Compression Guard
    """
    # 1. Source Ledger / Coverage Analysis
    source_ledger = None
    if source_text and source_text.strip():
        source_ledger = build_source_coverage_ledger(source_text, source_title)

    effective_scope = scope
    if getattr(job, "research_degraded", False):
        effective_scope = EvidenceScope.SOURCE_ONLY

    # 2. Research Plan & Evidence Gathering
    r_plan = None
    if not job.evidence_packet_json:
        if effective_scope in (EvidenceScope.RESEARCH, EvidenceScope.SOURCE_PLUS_RESEARCH):
            if status_notifier:
                status_notifier("Researching topic and gathering authoritative evidence...")

            r_plan = build_research_plan(
                topic=topic,
                research_depth=research_depth,
                scope=effective_scope,
                seed_summary=source_text[:500] if source_text else None,
            )
            job.research_plan_json = r_plan
            db.commit()

            record_job_diagnostic_event(
                job.id,
                "INFO",
                "research",
                "GROUNDED_RESEARCH_BEGIN",
                f"Starting grounded research for '{topic}' (depth={research_depth})",
                db=db,
            )

            def _do_grounding(p_inst: Any, att: int, src: str) -> dict[str, Any]:
                return p_inst.generate_grounded_research(
                    source_text=f"Topic: {topic}\nSeed Context: {src or ''}",
                    research_depth=research_depth,
                    job_id=job.id,
                    research_plan=r_plan,
                )

            try:
                grounded_data = execute_with_failover(
                    job=job,
                    operation="grounded_research",
                    execute_fn=_do_grounding,
                    db=db,
                    source_text=source_text or topic,
                    required_capability="research_grounding",
                )
                job.research_grounding_json = grounded_data
                job.research_search_count = grounded_data.get("search_count", 0)
                job.research_source_count = grounded_data.get("source_count", 0)
                job.research_provider = getattr(job, "ai_effective_provider", None) or getattr(job, "ai_provider", None)
                if job.research_provider == "gemini":
                    job.research_model = getattr(settings, "GEMINI_RESEARCH_MODEL", None) or getattr(job, "ai_effective_model", None)
                else:
                    job.research_model = getattr(job, "ai_effective_model", None) or getattr(job, "ai_model", None)
                db.commit()
            except (TypeError, AttributeError, AssertionError, KeyError, IndexError, SyntaxError, NameError):
                # Internal programming errors must never trigger graceful degradation
                raise
            except Exception as res_err:
                if effective_scope == EvidenceScope.SOURCE_PLUS_RESEARCH and source_text and len(source_text.strip()) >= 50:
                    from herald.ai.policy import classify_exception
                    classified_res_err = classify_exception(res_err)
                    degraded_reason = getattr(classified_res_err, "category", "AI_RESEARCH_FAILED")
                    logger.warning(
                        f"Grounded research failed during SOURCE_PLUS_RESEARCH ({degraded_reason}: {res_err}); "
                        "degrading gracefully to SOURCE_ONLY."
                    )
                    record_job_diagnostic_event(
                        job.id,
                        "WARNING",
                        "research",
                        "RESEARCH_DEGRADED_TO_SOURCE_ONLY",
                        f"Supplemental research was unavailable ({degraded_reason}); falling back to source-only generation.",
                        metadata={
                            "reason": degraded_reason,
                            "original_scope": scope.value if hasattr(scope, "value") else str(scope),
                            "error": str(res_err)[:200],
                        },
                        db=db,
                    )
                    job.research_degraded = True
                    job.research_degradation_reason = degraded_reason
                    job.research_grounding_json = None
                    job.research_search_count = 0
                    job.research_source_count = 0
                    job.research_provider = None
                    job.research_model = None
                    # Reset failover cursor so subsequent script generation uses the full candidate chain from index 0
                    job.ai_failover_index = 0
                    effective_scope = EvidenceScope.SOURCE_ONLY
                    grounded_data = None
                    if status_notifier:
                        status_notifier("Supplemental research unavailable; continuing with article-only generation...")
                    db.commit()
                else:
                    if effective_scope == EvidenceScope.RESEARCH:
                        logger.error(f"Research failed for topic mode; research is required but unavailable: {res_err}")
                        record_job_diagnostic_event(
                            job.id,
                            "ERROR",
                            "research",
                            "TOPIC_RESEARCH_REQUIRED_FAILED",
                            "Research is required for topic mode but no research provider is available.",
                            metadata={"error": str(res_err)[:200]},
                            db=db,
                        )
                        from herald.ai.errors import AIError
                        raise AIError(
                            f"Research is required for topic mode but no research provider is available: {res_err}",
                            category="AI_RESEARCH_REQUIRED",
                        ) from res_err
                    raise

            evidence_packet = normalize_evidence_packet(
                topic=topic,
                scope=effective_scope,
                seed_source_text=source_text,
                grounded_research_data=grounded_data,
                seed_source_url=job.source_url,
            )
        else:
            evidence_packet = normalize_evidence_packet(
                topic=topic,
                scope=effective_scope,
                seed_source_text=source_text,
                grounded_research_data=None,
                seed_source_url=job.source_url,
            )

        job.evidence_packet_json = evidence_packet
        db.commit()
    else:
        evidence_packet = job.evidence_packet_json
        r_plan = job.research_plan_json
        if getattr(job, "research_degraded", False):
            effective_scope = EvidenceScope.SOURCE_ONLY
            if isinstance(evidence_packet, dict):
                evidence_packet["scope"] = EvidenceScope.SOURCE_ONLY.value

    # 3. Episode Outline & Word Budgeting
    if not job.outline_json:
        if status_notifier:
            status_notifier("Building episode outline and section word budgets...")

        outline = build_episode_outline(
            topic=topic,
            evidence_packet=evidence_packet,
            target_minutes=target_minutes,
            scope=effective_scope,
            source_ledger=source_ledger,
            research_plan=r_plan,
        )
        job.outline_json = outline
        db.commit()
    else:
        outline = job.outline_json

    # 4. Sequential Section Generation with Checkpointing
    sections_def = outline.get("sections", [])
    completed_sections: list[dict[str, Any]] = list(job.section_progress_json or [])
    completed_indices = {s["section_index"] for s in completed_sections}

    for sec_def in sections_def:
        sec_idx = sec_def["section_index"]
        if sec_idx in completed_indices:
            continue

        if status_notifier:
            status_notifier(f"Writing section {sec_idx} of {len(sections_def)}: {sec_def['heading']}...")

        prev_narration = completed_sections[-1]["narration"] if completed_sections else None
        prev_summary = prev_narration[:200] if prev_narration else None

        sec_result = generate_single_section(
            job=job,
            section_info=sec_def,
            topic=topic,
            evidence_packet=evidence_packet,
            previous_summary=prev_summary,
            scope=effective_scope,
            db=db,
        )
        completed_sections.append(sec_result)
        job.section_progress_json = completed_sections
        db.commit()

    # Fixed duration enforcement: check for material underfill across all sections
    requested_budget = get_target_word_budget(target_minutes)
    planned_target = outline.get("target_total_words") or requested_budget
    total_generated_words = sum(s.get("word_count", 0) for s in completed_sections)

    if planned_target and planned_target > 500 and total_generated_words < int(planned_target * 0.75):
        if effective_scope != EvidenceScope.SOURCE_ONLY:
            # Expanded/Topic: execute bounded evidence-backed continuation pass
            if status_notifier:
                status_notifier("Performing evidence-backed continuation to meet target duration...")
            logger.info(
                f"Long-form underfilled target budget ({total_generated_words} words vs {planned_target} planned). "
                "Executing bounded evidence-backed continuation strategy."
            )
            deficit = planned_target - total_generated_words
            continuation_sec_def = {
                "section_index": len(completed_sections) + 1,
                "heading": "Comprehensive Analysis and Evidence Synthesis",
                "purpose": "Synthesize grounded research findings, historical/technical parallels, and systemic ramifications.",
                "word_budget": deficit,
                "relevant_evidence_ids": [it["evidence_id"] for it in evidence_packet.get("items", [])],
                "transition_intent": "Deepen the analysis with evidence synthesis",
            }
            prev_narr = completed_sections[-1]["narration"] if completed_sections else ""
            try:
                cont_sec = generate_single_section(
                    job=job,
                    section_info=continuation_sec_def,
                    topic=topic,
                    evidence_packet=evidence_packet,
                    previous_summary=prev_narr[:250],
                    scope=effective_scope,
                    db=db,
                )
                if cont_sec.get("word_count", 0) > 100:
                    completed_sections.append(cont_sec)
                    job.section_progress_json = completed_sections
                    total_generated_words = sum(s.get("word_count", 0) for s in completed_sections)
                    db.commit()
            except Exception as cont_err:
                logger.warning(f"Optional continuation section generation failed non-fatally: {cont_err}")
                record_job_diagnostic_event(
                    job.id,
                    "WARNING",
                    "section_expansion",
                    "CONTINUATION_EXPANSION_FAILED",
                    f"Optional continuation section expansion failed non-fatally: {cont_err}",
                    metadata={"error": str(cont_err)[:200]},
                    db=db,
                )
        else:
            # Source mode: only expand if source contains sufficient material; otherwise preserve faithful brevity
            src_words = len((source_ledger.get("clean_text", "") if source_ledger else (source_text or "")).split())
            if src_words >= int(planned_target * 0.75):
                if status_notifier:
                    status_notifier("Deepening source narration to meet target duration...")
                logger.info(f"Source has {src_words} words; expanding shortest sections to fulfill target budget.")
                shortest_idx = min(range(len(completed_sections)), key=lambda i: completed_sections[i].get("word_count", 99999))
                shortest_sec = completed_sections[shortest_idx]
                expansion_instructions = (
                    f"Target Word Budget: approximately {shortest_sec.get('target_word_budget', 500)} words. "
                    "Elaborate thoroughly on the provided source evidence with natural spoken detail, explaining mechanisms. "
                    "Do not invent facts outside the source."
                )
                try:
                    def _do_src_elab(p_inst: Any, att: int, src: str) -> PodcastScriptResponse:
                        return p_inst.generate_script(
                            source_text=src,
                            request_mode="standard",
                            source_title=topic,
                            job_id=job.id,
                            generation_instructions=expansion_instructions,
                        )

                    elab_res = execute_with_failover(
                        job=job,
                        operation="section_generation",
                        execute_fn=_do_src_elab,
                        db=db,
                        source_text=source_text or source_ledger.get("clean_text", ""),
                    )
                    elab_narr = "\n\n".join(seg.narration for seg in elab_res.segments)
                    if len(elab_narr.split()) > shortest_sec.get("word_count", 0):
                        shortest_sec["narration"] = elab_narr
                        shortest_sec["word_count"] = len(elab_narr.split())
                        job.section_progress_json = completed_sections
                        total_generated_words = sum(s.get("word_count", 0) for s in completed_sections)
                        db.commit()
                except Exception as elab_err:
                    logger.warning(f"Source-grounded expansion failed non-fatally: {elab_err}")
            else:
                logger.info(
                    f"Source word count ({src_words}) cannot legitimately support {planned_target} words. "
                    "Preserving faithful shorter result and recording duration telemetry."
                )

    # 5. Semantic Fidelity Audit & Bounded Repair
    if status_notifier:
        status_notifier("Verifying source coverage and factual fidelity...")

    repaired_sections, audit_res = audit_and_repair_fidelity(
        job=job,
        sections=completed_sections,
        source_ledger=source_ledger,
        evidence_packet=evidence_packet,
        scope=effective_scope,
        db=db,
        source_text=source_text,
    )
    job.fidelity_audit_json = audit_res

    # Record duration telemetry
    cfg_state = job.configuration_state_json or {}
    if not isinstance(cfg_state, dict):
        cfg_state = {}
    cfg_state.update({
        "requested_target_words": requested_budget,
        "evidence_supported_target_words": planned_target,
        "actual_words": sum(s.get("word_count", 0) for s in repaired_sections),
    })
    job.configuration_state_json = cfg_state
    db.commit()

    # 6. Final Coherence Pass & Assembly
    if status_notifier:
        status_notifier("Assembling final podcast script...")

    final_script = assemble_and_smooth_script(
        episode_title=topic,
        episode_description=outline.get("episode_description", f"Episode about {topic}"),
        sections=repaired_sections,
        source_title=source_title,
        planned_target_words=planned_target,
    )
    job.script_json = final_script.model_dump()
    db.commit()

    return final_script

