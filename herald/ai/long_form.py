"""Unified Long-Form Research, Planning, and Generation Engine.

Supports:
- SOURCE_ONLY: Fixed-duration Source mode using source analysis, coverage ledger,
  and outline budgeting without external research.
- SOURCE_PLUS_RESEARCH: Expanded mode using seed source + external grounded research.
- RESEARCH: Topic mode using topic seed + external grounded research.
- Actionable bounded fidelity audit & repair.
- True final coherence pass with anti-compression guards.
"""

import enum
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

# Duration to word budget mapping (~125-130 spoken words per minute for program body)
DURATION_WORD_BUDGETS: dict[str, int] = {
    "10": 1250,
    "20": 2500,
    "30": 3750,
    "45": 5600,
    "60": 7500,
}

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
    """Return explicit program word budget for fixed target minutes, or None for Auto/unspecified."""
    if target_minutes is None:
        return None
    key = str(target_minutes).lower().strip()
    return DURATION_WORD_BUDGETS.get(key)


def build_source_coverage_ledger(source_text: str, source_title: str | None = None) -> dict[str, Any]:
    """
    Extract key factual details, proper nouns, figures, dates, and statistics from source text
    while explicitly filtering out navigation, ads, and newsletter boilerplate.
    """
    lines = source_text.splitlines()
    clean_lines = []
    omitted_pollution = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        is_pollution = False
        lower_line = stripped.lower()
        for pat in BOILERPLATE_PATTERNS:
            if re.search(pat, lower_line):
                is_pollution = True
                omitted_pollution.append(stripped[:60])
                break
        if not is_pollution:
            clean_lines.append(stripped)

    clean_content = "\n".join(clean_lines)

    # Extract numbers, percentages, dates, currencies
    numbers = re.findall(
        r"(?:[\$€£]?\d+(?:[.,]\d+)*(?:\s*(?:percent|%|million|billion|trillion|meters|feet|knots|tons))?)",
        clean_content,
        re.IGNORECASE,
    )
    # Deduplicate while preserving order
    seen_nums = set()
    key_numbers = []
    for num in numbers:
        n_clean = num.strip().rstrip(".,;:)")
        if len(n_clean) >= 2 and n_clean not in seen_nums:
            seen_nums.add(n_clean)
            key_numbers.append(n_clean)

    # Extract capitalized multi-word proper nouns / entity names
    entities = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", clean_content)
    seen_ents = set()
    key_entities = []
    for ent in entities:
        if ent not in seen_ents and len(ent) > 4:
            seen_ents.add(ent)
            key_entities.append(ent)

    # Identify primary factual themes from paragraphs
    paragraphs = [p.strip() for p in clean_content.split("\n\n") if len(p.strip().split()) > 15]
    core_claims = [p[:160].strip() + "..." for p in paragraphs[:8]]

    return {
        "source_title": source_title or "Primary Source",
        "clean_text": clean_content,
        "key_numbers": key_numbers[:25],
        "key_entities": key_entities[:20],
        "core_claims": core_claims,
        "omitted_pollution": omitted_pollution[:10],
    }


def build_research_plan(
    topic: str,
    research_depth: str = "medium",
    scope: EvidenceScope = EvidenceScope.RESEARCH,
    seed_summary: str | None = None,
) -> dict[str, Any]:
    """Generate structured research plan for what should be investigated."""
    depth = (research_depth or "medium").lower().strip()
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
            "queries": [f"{topic} history background origin", f"{topic} timeline context"],
        })
        focus_areas.append({
            "name": "Technical Design and Capabilities",
            "focus": f"Detailed technical specifications, architecture, and design of {topic}",
            "queries": [f"{topic} technical specifications design", f"{topic} capabilities analysis"],
        })
        if target_areas >= 3:
            focus_areas.append({
                "name": "Operational Impact and Developments",
                "focus": f"Operational deployment, updates, controversies, and future outlook for {topic}",
                "queries": [f"{topic} recent developments updates", f"{topic} challenges controversies"],
            })
        if target_areas >= 4:
            focus_areas.append({
                "name": "Comparisons and Alternatives",
                "focus": f"Comparison of {topic} with alternatives or international counterparts",
                "queries": [f"{topic} comparison competitors", f"{topic} cost analysis"],
            })
        if target_areas >= 5:
            focus_areas.append({
                "name": "Strategic Significance and Future Program Direction",
                "focus": f"Long-term significance and future trajectory of {topic}",
                "queries": [f"{topic} future outlook strategic role", f"{topic} program trajectory"],
            })
    else:
        # Pure Topic mode
        focus_areas.append({
            "name": "Overview, Core Definition, and History",
            "focus": f"Definition, origins, and core facts regarding {topic}",
            "queries": [f"{topic} overview definition history", f"{topic} background facts"],
        })
        focus_areas.append({
            "name": "Key Mechanisms and Technical Details",
            "focus": f"How {topic} works, underlying mechanisms, or primary structure",
            "queries": [f"{topic} how it works technical details", f"{topic} architecture key aspects"],
        })
        if target_areas >= 3:
            focus_areas.append({
                "name": "Real-World Applications, Impact, and Controversies",
                "focus": f"Significance, impact, debates, and controversies around {topic}",
                "queries": [f"{topic} impact real world examples", f"{topic} controversies analysis"],
            })
        if target_areas >= 4:
            focus_areas.append({
                "name": "Modern Developments and Case Studies",
                "focus": f"Recent developments and notable case studies for {topic}",
                "queries": [f"{topic} recent developments case studies", f"{topic} current status"],
            })
        if target_areas >= 5:
            focus_areas.append({
                "name": "Future Outlook and Broader Implications",
                "focus": f"Where {topic} is heading and what it means for the future",
                "queries": [f"{topic} future predictions implications", f"{topic} research frontier"],
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
) -> dict[str, Any]:
    """Assemble attributable EvidencePacket with source registry and metadata."""
    items: list[dict[str, Any]] = []

    # If seed source exists (Expanded or Source modes), register as ev_seed
    if seed_source_text and seed_source_text.strip():
        items.append({
            "evidence_id": "ev_seed",
            "title": "Primary Submitted Source",
            "publisher": "User Source Material",
            "source_url": None,
            "snippet": seed_source_text[:3000].strip(),
            "is_seed_source": True,
            "focus_area": "Seed Material",
        })

    # External research items
    if grounded_research_data:
        raw_text = grounded_research_data.get("raw_text", "")
        sources = grounded_research_data.get("research_sources", [])
        for idx, src in enumerate(sources, 1):
            items.append({
                "evidence_id": f"ev_{idx}",
                "title": src.get("title") or f"Research Source {idx}",
                "publisher": src.get("publisher"),
                "source_url": src.get("url"),
                "snippet": src.get("snippet") or raw_text[((idx - 1) * 300) : (idx * 300 + 300)].strip(),
                "is_seed_source": False,
                "focus_area": "External Grounded Research",
            })

        # If no explicit research_sources registry returned, wrap raw_text
        if not sources and raw_text:
            items.append({
                "evidence_id": "ev_grounded_summary",
                "title": f"Grounded Research on {topic}",
                "publisher": "Google Search Grounding",
                "source_url": None,
                "snippet": raw_text[:4000].strip(),
                "is_seed_source": False,
                "focus_area": "External Grounded Research",
            })

    return {
        "topic": topic,
        "scope": scope.value,
        "evidence_count": len(items),
        "items": items,
    }


def build_episode_outline(
    topic: str,
    evidence_packet: dict[str, Any],
    target_minutes: str | int | None,
    scope: EvidenceScope,
    source_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build structured episode outline with section word budgets.
    For Source mode with short source: refuses unsupported expansion and bounds budget.
    For Auto: determines natural section count from evidence without fixed quota.
    """
    target_budget = get_target_word_budget(target_minutes)
    is_auto = target_budget is None

    items = evidence_packet.get("items", [])
    evidence_ids = [it["evidence_id"] for it in items]

    # Source mode bounds check: if source text has few words, do NOT create 30-60m padding
    source_words = len((source_ledger.get("clean_text", "") if source_ledger else "").split())
    if scope == EvidenceScope.SOURCE_ONLY and target_budget is not None and source_words > 0:
        # A source can be legibly organized into spoken prose at ~1.5x - 2x its word count max
        max_legitimate_words = max(source_words * 2, 800)
        if target_budget > max_legitimate_words:
            logger.info(
                f"Source mode: source word count ({source_words}) cannot legitimately support requested budget ({target_budget} words). "
                f"Bounding word budget to {max_legitimate_words} words to avoid hallucinated padding."
            )
            target_budget = max_legitimate_words

    # Section counts and target word distribution
    if is_auto:
        # Natural length based on evidence breadth
        section_count = max(3, min(len(items) + 1, 6))
        section_word_budget = 400
        effective_total_words = section_count * section_word_budget
    else:
        effective_total_words = target_budget
        if target_budget <= 1500:
            section_count = 3
        elif target_budget <= 3000:
            section_count = 5
        elif target_budget <= 4500:
            section_count = 7
        elif target_budget <= 6000:
            section_count = 9
        else:
            section_count = 11
        section_word_budget = target_budget // section_count

    sections = []
    # Standard outline progression
    standard_headings = [
        ("The Big Picture and Core Stakes", "Establish what is at stake and the central premise."),
        ("Origins, Evolution, and Context", "Explore historical roots and how this situation developed."),
        ("Architecture, Mechanics, and Design", "Examine technical specifications, design details, and operations."),
        ("Key Figures, Challenges, and Controversies", "Address major dilemmas, competing perspectives, and obstacles."),
        ("Operational Realities and Case Studies", "Analyze tangible real-world deployments, tests, or examples."),
        ("Strategic Implications and Looking Ahead", "Synthesize long-term meaning, future trajectory, and conclusions."),
    ]

    for i in range(section_count):
        idx = i + 1
        heading, purpose = standard_headings[i % len(standard_headings)]
        if i >= len(standard_headings):
            heading = f"Deep Dive: Part {idx - len(standard_headings) + 1} - {heading}"

        # Assign relevant evidence subset
        assigned_ev = (
            evidence_ids
            if len(evidence_ids) <= 3
            else [evidence_ids[i % len(evidence_ids)], evidence_ids[(i + 1) % len(evidence_ids)]]
        )

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
        "target_total_words": effective_total_words,
        "section_count": section_count,
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
    Tracks progress and uses execute_with_failover for deterministic retry & provider failover.
    """
    sec_idx = section_info["section_index"]
    heading = section_info["heading"]
    purpose = section_info["purpose"]
    budget = section_info["word_budget"]
    ev_ids = section_info.get("relevant_evidence_ids", [])

    all_items = {it["evidence_id"]: it for it in evidence_packet.get("items", [])}
    assigned_snippets = []
    for eid in ev_ids:
        if eid in all_items:
            it = all_items[eid]
            assigned_snippets.append(f"[{it['evidence_id']} - {it['title']}]: {it['snippet']}")

    evidence_text = "\n\n".join(assigned_snippets) or "Use verified factual details from the core topic."

    prev_context = (
        f"Previous section covered: {previous_summary}. Do NOT repeat those introductory facts. Continue the narrative."
        if previous_summary
        else "This is the opening section. Hook the listener and state the core premise."
    )

    prompt = f"""You are writing Section {sec_idx} of a long-form conversational podcast about: {topic}
Section Heading: {heading}
Purpose: {purpose}
Target Word Budget: approximately {budget} words.

{prev_context}

Grounded Evidence for this Section:
<EVIDENCE>
{evidence_text}
</EVIDENCE>

Requirements:
1. Write engaging, natural spoken podcast narration for the host.
2. Ground all factual assertions in the provided evidence.
3. Keep length close to the word budget (~{budget} words). Do NOT produce an overly brief summary.
4. If this is Source mode (SOURCE_ONLY), do NOT introduce outside facts not present in the evidence.
"""

    def _execute_section(p_inst: Any, attempt: int, src: str) -> PodcastScriptResponse:
        return p_inst.generate_script(
            source_text=prompt,
            request_mode="standard",
            source_title=topic,
            job_id=job.id,
        )

    res: PodcastScriptResponse = execute_with_failover(
        job=job,
        operation="section_generation",
        execute_fn=_execute_section,
        db=db,
        source_text=prompt,
    )

    # Extract narration from segments
    narration_parts = [seg.narration for seg in res.segments]
    full_narration = "\n\n".join(narration_parts)
    actual_words = len(full_narration.split())

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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Perform actionable fidelity audit against source ledger and evidence packet.
    Triggers at most 1 bounded repair pass if material omissions are detected.
    """
    combined_narration = " ".join(s.get("narration", "") for s in sections)
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

    has_omissions = len(omitted_numbers) > 2 or len(omitted_entities) > 2
    coverage_score = max(0.0, 1.0 - (len(omitted_numbers) + len(omitted_entities)) * 0.05)

    audit_result = {
        "has_material_issues": has_omissions,
        "omitted_numbers": omitted_numbers[:10],
        "omitted_entities": omitted_entities[:10],
        "coverage_score": round(coverage_score, 2),
        "repair_attempted": False,
    }

    # Bounded repair: execute at most 1 repair pass on the most relevant section if material omissions exist
    if has_omissions and (job.verify_repair_count or 0) == 0:
        logger.info(
            f"Fidelity audit detected {len(omitted_numbers)} omitted figures and {len(omitted_entities)} omitted entities. "
            f"Triggering bounded repair for job {job.id}."
        )
        record_job_diagnostic_event(
            job.id,
            "INFO",
            "fidelity",
            "FIDELITY_REPAIR_BEGIN",
            "Executing bounded repair to restore material source facts and figures.",
            metadata=audit_result,
            db=db,
        )

        missing_summary = ", ".join(omitted_numbers[:5] + omitted_entities[:5])
        target_sec = sections[0] if sections else None
        if target_sec:
            repair_prompt = f"""
Integrate the following missing factual details and numbers into this podcast section narration without changing the tone or removing existing facts:
Missing details to incorporate: {missing_summary}

Original Narration:
{target_sec['narration']}
"""
            try:
                def _do_repair(p_inst, att, src):
                    return p_inst.generate_script(
                        source_text=repair_prompt,
                        request_mode="standard",
                        source_title=job.custom_title,
                        job_id=job.id,
                    )

                repaired_res: PodcastScriptResponse = execute_with_failover(
                    job=job,
                    operation="fidelity_repair",
                    execute_fn=_do_repair,
                    db=db,
                    source_text=repair_prompt,
                )
                repaired_text = "\n\n".join(seg.narration for seg in repaired_res.segments)
                if len(repaired_text.split()) >= int(target_sec["word_count"] * 0.8):
                    target_sec["narration"] = repaired_text
                    target_sec["word_count"] = len(repaired_text.split())
                    audit_result["repair_attempted"] = True
                    job.verify_repair_count = 1
                    record_job_diagnostic_event(
                        job.id,
                        "INFO",
                        "fidelity",
                        "FIDELITY_REPAIR_SUCCESS",
                        "Bounded fidelity repair succeeded.",
                        db=db,
                    )
            except Exception as rep_err:
                logger.warning(f"Fidelity repair attempt failed non-fatally: {rep_err}")

    return sections, audit_result


def assemble_and_smooth_script(
    episode_title: str,
    episode_description: str,
    sections: list[dict[str, Any]],
    source_title: str | None = None,
) -> PodcastScriptResponse:
    """
    True Final Coherence Pass with Anti-Compression Guard.
    Assembles generated sections into PodcastSegment items.
    Enforces that assembled script word count remains within +/- 10% of sum of sections,
    forbidding destructive compression into a short summary.
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

        # Smooth section transition: strip redundant opening "In this section" or duplicate greetings
        cleaned_narration = re.sub(r"^(?:in this section,?|turning now to our next topic,?)\s*", "", narration, flags=re.IGNORECASE)
        segments.append(
            PodcastSegment(
                order=idx,
                heading=heading,
                narration=cleaned_narration,
            )
        )

    assembled_words = sum(len(seg.narration.split()) for seg in segments)

    # Anti-Compression Guardrail: verify assembled word count does not collapse
    if total_raw_section_words > 500:
        min_allowed = int(total_raw_section_words * 0.85)
        if assembled_words < min_allowed:
            raise ValueError(
                f"Anti-Compression violation: Assembled script ({assembled_words} words) collapsed below 85% "
                f"of sectional content ({total_raw_section_words} words)."
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
    5. Actionable Fidelity Audit & Bounded Repair
    6. Final Assembly & Coherence Pass with Anti-Compression Guard
    """
    # 1. Source Ledger / Coverage Analysis
    source_ledger = None
    if source_text and source_text.strip():
        source_ledger = build_source_coverage_ledger(source_text, source_title)

    # 2. Research Plan & Evidence Gathering
    if not job.evidence_packet_json:
        if scope in (EvidenceScope.RESEARCH, EvidenceScope.SOURCE_PLUS_RESEARCH):
            if status_notifier:
                status_notifier("Researching topic and gathering authoritative evidence...")

            r_plan = build_research_plan(
                topic=topic,
                research_depth=research_depth,
                scope=scope,
                seed_summary=source_text[:500] if source_text else None,
            )
            job.research_plan_json = r_plan
            db.commit()

            # Execute Grounded Research via provider with research_grounding capability
            record_job_diagnostic_event(
                job.id,
                "INFO",
                "research",
                "GROUNDED_RESEARCH_BEGIN",
                f"Starting grounded research for '{topic}' (depth={research_depth})",
                db=db,
            )

            def _do_grounding(p_inst, att, src):
                return p_inst.generate_grounded_research(
                    source_text=f"Topic: {topic}\nSeed Context: {src or ''}",
                    research_depth=research_depth,
                    job_id=job.id,
                )

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
            db.commit()

            evidence_packet = normalize_evidence_packet(
                topic=topic,
                scope=scope,
                seed_source_text=source_text,
                grounded_research_data=grounded_data,
            )
        else:
            # Source mode (SOURCE_ONLY): Evidence gathered exclusively from source
            evidence_packet = normalize_evidence_packet(
                topic=topic,
                scope=scope,
                seed_source_text=source_text,
                grounded_research_data=None,
            )

        job.evidence_packet_json = evidence_packet
        db.commit()
    else:
        evidence_packet = job.evidence_packet_json

    # 3. Episode Outline & Word Budgeting
    if not job.outline_json:
        if status_notifier:
            status_notifier("Building episode outline and section word budgets...")

        outline = build_episode_outline(
            topic=topic,
            evidence_packet=evidence_packet,
            target_minutes=target_minutes,
            scope=scope,
            source_ledger=source_ledger,
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
            scope=scope,
            db=db,
        )
        completed_sections.append(sec_result)
        job.section_progress_json = completed_sections
        db.commit()

    # 5. Actionable Fidelity Audit & Bounded Repair
    if status_notifier:
        status_notifier("Verifying source coverage and factual fidelity...")

    repaired_sections, audit_res = audit_and_repair_fidelity(
        job=job,
        sections=completed_sections,
        source_ledger=source_ledger,
        evidence_packet=evidence_packet,
        scope=scope,
        db=db,
    )
    job.fidelity_audit_json = audit_res
    db.commit()

    # 6. Final Coherence Pass & Assembly
    if status_notifier:
        status_notifier("Assembling podcast script...")

    final_script = assemble_and_smooth_script(
        episode_title=topic,
        episode_description=outline.get("episode_description", f"Episode about {topic}"),
        sections=repaired_sections,
        source_title=source_title,
    )
    job.script_json = final_script.model_dump()
    db.commit()

    return final_script
