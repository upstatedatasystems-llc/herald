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
from herald.ai.schema import (
    MetadataCleanupResponse,
    PodcastScriptResponse,
    PodcastSegment,
    RepetitionReviewItem,
    RepetitionReviewResponse,
    parse_isolated_section_response,
)
from herald.config import settings
from herald.db.models import ContentMode, PodcastJob
from herald.services.diagnostic_recorder import record_job_diagnostic_event
from herald.services.quality_gate import COMMON_PHRASE_STOPWORDS, clean_metadata_scaffolding

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


def _detect_topic_domain(topic: str) -> str:
    """Classify topic domain to generate topic-specific narrative arcs."""
    t_lower = topic.lower()
    if any(k in t_lower for k in (
        "black hole", "singularity", "quantum", "space", "star", "gravity", "relativity",
        "fusion", "galaxy", "cosmology", "atom", "particle", "biology", "climate", "geology",
        "chemistry", "physics", "planet", "radiation", "astronomy", "neuroscience", "evolution"
    )):
        return "science"
    if any(k in t_lower for k in (
        "restaurant", "roadhouse", "company", "business", "market", "retail", "economy",
        "startup", "finance", "industry", "banking", "store", "supply chain", "logistics",
        "brand", "franchise", "commercial", "earnings", "valuation", "customer"
    )):
        return "business"
    if any(k in t_lower for k in (
        "software", "hardware", "computer", "algorithm", "ai", "network", "engine",
        "submarine", "reactor", "chip", "database", "system", "propulsion", "crypto",
        "security", "protocol", "architecture", "microprocessor", "compiler"
    )):
        return "technology"
    if any(k in t_lower for k in (
        "war", "revolution", "treaty", "empire", "policy", "movement", "presidency",
        "century", "battle", "crisis", "doctrine", "rebellion", "dynasty", "colonial"
    )):
        return "history"
    if any(k in t_lower for k in (
        "biography", "life of", "who was", "inventor", "admiral", "general", "founder", "biographical"
    )):
        return "biography"
    return "general"


def build_research_plan(
    topic: str,
    research_depth: str = "medium",
    scope: EvidenceScope = EvidenceScope.RESEARCH,
    seed_summary: str | None = None,
) -> dict[str, Any]:
    """Generate structured research plan for what should be investigated, tailored to the specific topic domain."""
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

    domain = _detect_topic_domain(topic)
    focus_areas = []

    if domain == "science":
        focus_areas.extend([
            {
                "name": "Formation Mechanisms and Physical Genesis",
                "focus": f"Cosmic formation mechanisms, theoretical foundations, and genesis of {topic}",
                "queries": [f"{topic} formation origin physics", f"{topic} theoretical predictions discovery"][:queries_per_area],
            },
            {
                "name": "Governing Principles and Observable Mechanics",
                "focus": f"Physical laws, observable behaviors, and fundamental mechanics of {topic}",
                "queries": [f"{topic} physical properties mechanisms", f"{topic} structure dynamics"][:queries_per_area],
            },
            {
                "name": "Observation Milestones and Experimental Evidence",
                "focus": f"Empirical measurements, instrumentation breakthroughs, and observational evidence for {topic}",
                "queries": [f"{topic} observation telescope detector", f"{topic} experimental findings evidence"][:queries_per_area],
            },
            {
                "name": "Complex Interactions and Environmental Dynamics",
                "focus": f"Interactions, environmental impact, and dynamic growth phenomena of {topic}",
                "queries": [f"{topic} interaction behavior environment", f"{topic} growth dynamics anomalies"][:queries_per_area],
            },
            {
                "name": "Frontiers, Paradoxes, and Cosmological Horizon",
                "focus": f"Unresolved paradoxes, open questions, and broader cosmological significance of {topic}",
                "queries": [f"{topic} paradox open questions future", f"{topic} research frontier implications"][:queries_per_area],
            },
        ])
    elif domain == "business":
        focus_areas.extend([
            {
                "name": "Founding Vision, Origin, and Concept",
                "focus": f"Founding history, entrepreneurial vision, and initial operating concept of {topic}",
                "queries": [f"{topic} founding history origin", f"{topic} concept initial launch"][:queries_per_area],
            },
            {
                "name": "Operating Model and Customer Experience Strategy",
                "focus": f"Core operational strategy, execution model, product quality, and customer service for {topic}",
                "queries": [f"{topic} business model operations", f"{topic} service strategy execution"][:queries_per_area],
            },
            {
                "name": "Unit Economics, Scaling, and Partnership Model",
                "focus": f"Financial structure, store/unit economics, leadership incentives, and scaling strategy of {topic}",
                "queries": [f"{topic} unit economics financials", f"{topic} scaling strategy expansion"][:queries_per_area],
            },
            {
                "name": "Market Competition, Culture, and Challenges",
                "focus": f"Competitive landscape, internal organizational culture, labor relations, and operational challenges of {topic}",
                "queries": [f"{topic} competition industry challenges", f"{topic} organizational culture labor"][:queries_per_area],
            },
            {
                "name": "Modern Evolution and Strategic Horizon",
                "focus": f"Current market performance, adaptation to industry trends, and future strategic trajectory of {topic}",
                "queries": [f"{topic} current market outlook", f"{topic} future growth trajectory"][:queries_per_area],
            },
        ])
    elif domain == "technology":
        focus_areas.extend([
            {
                "name": "Engineering Genesis and Core Problem Space",
                "focus": f"Technical genesis, preceding limitations, and foundational problem space addressed by {topic}",
                "queries": [f"{topic} origins design purpose", f"{topic} engineering background problem"][:queries_per_area],
            },
            {
                "name": "System Architecture and Technical Specifications",
                "focus": f"Underlying architecture, hardware/software specifications, and technical mechanics of {topic}",
                "queries": [f"{topic} technical specifications architecture", f"{topic} design engineering details"][:queries_per_area],
            },
            {
                "name": "Operational Performance and Real-World Deployments",
                "focus": f"Deployment history, operational benchmarks, field reliability, and real-world performance of {topic}",
                "queries": [f"{topic} benchmarks deployment performance", f"{topic} operational reliability testing"][:queries_per_area],
            },
            {
                "name": "Engineering Tradeoffs and Competitive Alternatives",
                "focus": f"Tradeoffs, economic and technical costs, failure modes, and alternative approaches compared to {topic}",
                "queries": [f"{topic} tradeoffs alternatives comparison", f"{topic} limitations engineering challenges"][:queries_per_area],
            },
            {
                "name": "Emerging Frontiers and Future Roadmap",
                "focus": f"Next-generation revisions, research frontiers, and long-term technological trajectory of {topic}",
                "queries": [f"{topic} next generation future roadmap", f"{topic} emerging research frontier"][:queries_per_area],
            },
        ])
    elif domain == "history":
        focus_areas.extend([
            {
                "name": "Historical Catalysts and Preconditions",
                "focus": f"Underlying historical causes, socio-economic context, and catalysts leading to {topic}",
                "queries": [f"{topic} causes background context", f"{topic} historical roots catalysts"][:queries_per_area],
            },
            {
                "name": "Key Turning Points and Decision-Makers",
                "focus": f"Crucial decisions, major milestones, and influential actors during {topic}",
                "queries": [f"{topic} key figures turning points", f"{topic} major decisions chronology"][:queries_per_area],
            },
            {
                "name": "Structural Dynamics and Societal Impact",
                "focus": f"Everyday reality, societal shifts, and systemic consequences of {topic}",
                "queries": [f"{topic} societal impact consequences", f"{topic} structural changes effect"][:queries_per_area],
            },
            {
                "name": "Resolution, Aftermath, and Immediate Fallout",
                "focus": f"Treaties, settlements, immediate repercussions, and structural aftermath of {topic}",
                "queries": [f"{topic} aftermath treaty outcome", f"{topic} immediate consequences resolution"][:queries_per_area],
            },
            {
                "name": "Enduring Lessons and Historiographical Legacy",
                "focus": f"Modern historiographical perspectives, lasting lessons, and historical legacy of {topic}",
                "queries": [f"{topic} legacy historical debate", f"{topic} modern significance lessons"][:queries_per_area],
            },
        ])
    else:
        # General / default domain
        focus_areas.extend([
            {
                "name": f"Foundational Context and Core Premise of {topic}",
                "focus": f"Core premise, historical context, and foundational realities of {topic}",
                "queries": [f"{topic} overview foundation history", f"{topic} core premise background"][:queries_per_area],
            },
            {
                "name": "Underlying Mechanisms and Functional Structure",
                "focus": f"How {topic} operates, primary components, and functional dynamics",
                "queries": [f"{topic} how it works mechanisms", f"{topic} key elements dynamics"][:queries_per_area],
            },
            {
                "name": "Real-World Impact and Case Studies",
                "focus": f"Concrete manifestations, real-world case studies, and practical examples of {topic}",
                "queries": [f"{topic} real world examples case studies", f"{topic} practical impact evidence"][:queries_per_area],
            },
            {
                "name": "Tradeoffs, Nuances, and Critical Debates",
                "focus": f"Tensions, controversies, limitations, and competing viewpoints surrounding {topic}",
                "queries": [f"{topic} controversies debates nuance", f"{topic} challenges tradeoffs"][:queries_per_area],
            },
            {
                "name": "Strategic Horizon and Broader Implications",
                "focus": f"Where {topic} is heading, emerging trends, and broader long-term ramifications",
                "queries": [f"{topic} future outlook trajectory", f"{topic} long term implications"][:queries_per_area],
            },
        ])

    return {
        "topic": topic,
        "research_depth": depth,
        "scope": scope.value,
        "domain": domain,
        "focus_areas": focus_areas[:target_areas],
        "seed_summary": seed_summary,
    }


def extract_narrative_plan_from_research_text(raw_text: str | None) -> list[dict[str, Any]]:
    """
    Extract structured, topic-specific narrative plan from grounded research text.
    Searches for <NARRATIVE_PLAN>...</NARRATIVE_PLAN> or JSON array blocks containing
    section headings and narrative purposes.
    Returns list of dicts with keys: heading, purpose, key_points, relevant_sources.
    """
    if not raw_text or not isinstance(raw_text, str):
        return []

    text_to_parse = raw_text

    # 1. Try matching <NARRATIVE_PLAN>...</NARRATIVE_PLAN>
    plan_match = re.search(r"<NARRATIVE_PLAN>(.*?)</NARRATIVE_PLAN>", raw_text, re.DOTALL | re.IGNORECASE)
    if plan_match:
        text_to_parse = plan_match.group(1).strip()

    # 2. Extract JSON array
    json_array_match = re.search(r"\[\s*\{.*\}\s*\]", text_to_parse, re.DOTALL)
    candidate_json = json_array_match.group(0) if json_array_match else text_to_parse

    parsed_data = None
    try:
        parsed_data = json.loads(candidate_json)
    except Exception:
        clean_cand = re.sub(r"^```(?:json)?\s*", "", candidate_json.strip())
        clean_cand = re.sub(r"\s*```$", "", clean_cand.strip())
        try:
            parsed_data = json.loads(clean_cand)
        except Exception:
            pass

    if not isinstance(parsed_data, list):
        return []

    valid_sections: list[dict[str, Any]] = []
    for item in parsed_data:
        if not isinstance(item, dict):
            continue
        h = item.get("heading")
        p = item.get("purpose")
        if not h or not isinstance(h, str) or not p or not isinstance(p, str):
            continue

        clean_heading = re.sub(r"^(?:section\s+\d+[:.]?|\d+[\).:-])\s*", "", h.strip(), flags=re.IGNORECASE)
        clean_purpose = p.strip()

        kps = item.get("key_points") or []
        if isinstance(kps, str):
            kps = [k.strip() for k in re.split(r"[;\n]+", kps) if k.strip()]
        elif not isinstance(kps, list):
            kps = []
        clean_kps = [str(k).strip() for k in kps if str(k).strip()]

        rel_sources = item.get("relevant_sources") or item.get("relevant_evidence_ids") or []
        if isinstance(rel_sources, str):
            rel_sources = [s.strip() for s in re.split(r"[,;\s]+", rel_sources) if s.strip()]
        elif not isinstance(rel_sources, list):
            rel_sources = []
        clean_sources = [str(s).strip() for s in rel_sources if str(s).strip()]

        valid_sections.append({
            "heading": clean_heading,
            "purpose": clean_purpose,
            "key_points": clean_kps[:4],
            "relevant_sources": clean_sources,
            "relevant_evidence_ids": clean_sources,
        })

    return valid_sections


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
            web_queries = grounding_meta.get("webSearchQueries") or grounding_meta.get("web_search_queries") or []
            for s_idx, supp in enumerate(grounding_supports, 1):
                seg = supp.get("segment", {})
                claim_text = seg.get("text", "").strip()
                chunk_indices = supp.get("groundingChunkIndices", [])
                supp_sources = []
                supp_titles = []
                supp_publishers = []
                for c_idx in chunk_indices:
                    if 0 <= c_idx < len(grounding_chunks):
                        g_chunk = grounding_chunks[c_idx]
                        web = g_chunk.get("web", {}) if isinstance(g_chunk, dict) else {}
                        u = web.get("uri") or web.get("url")
                        t = web.get("title")
                        pub = web.get("publisher") or web.get("domain")
                        if u:
                            supp_sources.append(u)
                        if t:
                            supp_titles.append(t)
                        if pub:
                            supp_publishers.append(pub)

                if claim_text:
                    first_src_url = supp_sources[0] if supp_sources else None
                    first_src_title = supp_titles[0] if supp_titles else None
                    first_pub = supp_publishers[0] if supp_publishers else None
                    items.append({
                        "evidence_id": f"ev_ground_{s_idx}",
                        "title": first_src_title or f"Grounded Finding {s_idx}",
                        "actual_source_title": first_src_title,
                        "finding_number": s_idx,
                        "publisher": first_pub or "Google Search Grounding",
                        "source_url": first_src_url,
                        "source_ids": [f"S{c+1}" for c in chunk_indices],
                        "snippet": claim_text,
                        "is_seed_source": False,
                        "focus_area": "External Grounded Research",
                        "search_query": web_queries[0] if web_queries else None,
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

    # Extract / preserve topic-specific narrative plan from grounded research if present
    narrative_plan: list[dict[str, Any]] = []
    if grounded_research_data:
        plan_data = grounded_research_data.get("narrative_plan")
        if plan_data and isinstance(plan_data, list):
            narrative_plan = plan_data
        elif grounded_research_data.get("raw_text"):
            narrative_plan = extract_narrative_plan_from_research_text(grounded_research_data["raw_text"])

    # Semantically associate narrative plan sections with actual evidence items
    items_by_id = {it["evidence_id"]: it for it in items if it.get("evidence_id")}
    for plan_sec in narrative_plan:
        raw_sources = plan_sec.get("relevant_sources") or plan_sec.get("relevant_evidence_ids") or []
        matched_eids = []
        for s in raw_sources:
            if not s or not isinstance(s, str):
                continue
            s_clean = s.strip()
            # 1. Direct evidence ID match (validated against items_by_id)
            if s_clean in items_by_id:
                matched_eids.append(s_clean)
                continue
            s_lower = s_clean.lower()
            # 2. Source URL or domain match
            for it in items:
                it_url = (it.get("source_url") or "").lower()
                it_title = (it.get("title") or "").lower()
                it_eid = it["evidence_id"]
                if it_url and (s_lower in it_url or it_url in s_lower):
                    matched_eids.append(it_eid)
                elif len(s_clean) > 3 and (s_lower in it_title or it_title in s_lower):
                    matched_eids.append(it_eid)
                elif it.get("source_ids") and any(s_clean == sid for sid in it["source_ids"]):
                    matched_eids.append(it_eid)
        plan_sec["relevant_evidence_ids"] = list(dict.fromkeys(matched_eids))

    return {
        "topic": topic,
        "scope": scope.value,
        "seed_source_url": seed_source_url,
        "evidence_count": len(items),
        "items": items,
        "narrative_plan": narrative_plan,
    }


class CoverageLedger:
    """
    Compact cross-section coverage ledger.
    Retains structured facts:
    - major claims already explained (short snippets <= 60 chars)
    - evidence IDs consumed
    - key examples used
    - concepts introduced (should not be re-explained)
    Enforces a strict size guard (<= 1200 chars / ~250 tokens) with deterministic compaction.
    """

    def __init__(self):
        self.claims: list[dict[str, str]] = []
        self.consumed_evidence_ids: list[str] = []
        self.used_examples: list[str] = []
        self.introduced_concepts: list[str] = []

    def record_section(self, section_data: dict[str, Any]) -> None:
        # Extract evidence IDs
        for eid in section_data.get("relevant_evidence_ids", []):
            if eid and str(eid) not in self.consumed_evidence_ids:
                self.consumed_evidence_ids.append(str(eid))

        # Extract claims / key points
        kp = section_data.get("key_points") or []
        for p in kp:
            p_clean = str(p).strip()[:60]
            if p_clean and not any(c.get("snippet") == p_clean for c in self.claims):
                self.claims.append({
                    "section_index": str(section_data.get("section_index", "?")),
                    "snippet": p_clean,
                })

        # Extract concepts from heading
        h = section_data.get("heading")
        if h:
            h_clean = str(h).strip()[:40]
            if h_clean not in self.introduced_concepts:
                self.introduced_concepts.append(h_clean)

        # Extract examples if mentioned
        narr = section_data.get("narration", "")
        ex_matches = re.findall(r"(?:for example|such as|including)\s+([a-zA-Z0-9\s,]{4,35})[,.]", narr, re.IGNORECASE)
        for ex in ex_matches[:2]:
            ex_clean = ex.strip()[:35]
            if ex_clean and ex_clean not in self.used_examples:
                self.used_examples.append(ex_clean)

    def format_context(
        self,
        current_heading: str | None = None,
        current_purpose: str | None = None,
        current_idx: int | None = None,
        last_closing_sentence: str | None = None,
    ) -> str:
        lines = ["ALREADY COVERED IN PREVIOUS SECTIONS (COVERAGE LEDGER - ADVANCE WITHOUT RE-EXPLAINING):"]

        if self.introduced_concepts:
            c_slice = self.introduced_concepts[-5:]
            lines.append(f"- Concepts introduced: {', '.join(c_slice)}")

        if self.claims:
            c_slice = self.claims[-4:]
            c_strs = [f"Sec {c['section_index']}: {c['snippet']}" for c in c_slice]
            lines.append(f"- Claims covered: {'; '.join(c_strs)}")

        if self.used_examples:
            e_slice = self.used_examples[-3:]
            lines.append(f"- Examples already used: {', '.join(e_slice)}")

        if self.consumed_evidence_ids:
            lines.append(f"- Evidence consumed: {', '.join(self.consumed_evidence_ids[-8:])}")

        if last_closing_sentence:
            lines.append(f'Previous section ended with: "{last_closing_sentence}"')

        if current_heading and current_purpose:
            c_idx_str = f"Section {current_idx}" if current_idx is not None else "Current Section"
            lines.append(
                f"Your task for {c_idx_str} ({current_heading}):\n"
                f"Focus on {current_purpose}. Introduce new evidence. Do not repeat facts from above."
            )

        res = "\n".join(lines)
        if len(res) > 1200:
            res = res[:1150] + "\n[Ledger compacted for length]"
        return res


def build_already_covered_context(
    completed_sections: list[dict[str, Any]],
    current_heading: str | None = None,
    current_purpose: str | None = None,
    current_idx: int | None = None,
) -> str:
    """
    Deterministically build compact, bounded (~200-250 tokens) anti-repetition guidance
    using CoverageLedger to prevent narrative looping without LLM calls.
    """
    if not completed_sections:
        return ""

    ledger = CoverageLedger()
    for s in completed_sections:
        ledger.record_section(s)

    last_narr = completed_sections[-1].get("narration", "").strip()
    sentences = re.findall(r"[^.!?]+[.!?]+", last_narr)
    closing_sentence = sentences[-1].strip() if sentences else (last_narr[-120:].strip() if last_narr else "")
    if len(closing_sentence) > 120:
        closing_sentence = closing_sentence[-120:].strip()

    return ledger.format_context(
        current_heading=current_heading,
        current_purpose=current_purpose,
        current_idx=current_idx,
        last_closing_sentence=closing_sentence or None,
    )


def adapt_narrative_plan_to_generation_sections(
    narrative_plan: list[dict[str, Any]],
    target_count: int,
    items_by_id: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Deterministically adapt narrative chapters into generation sections.
    Separates narrative structure (determined by research) from generation granularity (determined by word budget).
    - If target_count == len(narrative_plan): 1-to-1 mapping
    - If target_count < len(narrative_plan): deterministically merges adjacent chapters
    - If target_count > len(narrative_plan): deterministically subdivides chapters using key points & evidence
    Zero LLM calls are made.
    """
    if not narrative_plan:
        return []

    items_by_id = items_by_id or {}
    n = len(narrative_plan)

    if target_count <= 0 or target_count == n:
        return [dict(c) for c in narrative_plan]

    if target_count < n:
        # Merge adjacent chapters into target_count generation sections
        merged_sections: list[dict[str, Any]] = []
        for i in range(target_count):
            start_idx = (i * n) // target_count
            end_idx = ((i + 1) * n) // target_count
            group = narrative_plan[start_idx:end_idx]

            if not group:
                continue

            if len(group) == 1:
                merged_sections.append(dict(group[0]))
            else:
                headings = [c.get("heading", "").strip() for c in group if c.get("heading")]
                if len(headings) == 2:
                    combined_heading = f"{headings[0]} & {headings[1]}" if headings[0] != headings[1] else headings[0]
                elif len(headings) > 2:
                    combined_heading = f"{headings[0]} through {headings[-1]}"
                else:
                    combined_heading = "Synthesized Section"

                purposes = [c.get("purpose", "").strip() for c in group if c.get("purpose")]
                combined_purpose = "; ".join(purposes) if purposes else f"Explore {combined_heading}."

                combined_kp = []
                for c in group:
                    for kp in c.get("key_points", []):
                        if kp and kp not in combined_kp:
                            combined_kp.append(kp)

                combined_eids = []
                for c in group:
                    for eid in c.get("relevant_evidence_ids", []):
                        if eid and eid not in combined_eids:
                            combined_eids.append(eid)

                merged_sections.append({
                    "heading": combined_heading,
                    "purpose": combined_purpose,
                    "key_points": combined_kp,
                    "relevant_evidence_ids": combined_eids,
                })
        return merged_sections

    # target_count > n: Subdivide chapters
    allocations = [1] * n
    extra_slots = target_count - n

    while extra_slots > 0:
        best_idx = 0
        best_score = -1e9
        for idx, c in enumerate(narrative_plan):
            kp_count = len(c.get("key_points", []))
            ev_count = len(c.get("relevant_evidence_ids", []))
            curr_alloc = allocations[idx]
            score = (kp_count + ev_count * 2) - (curr_alloc - 1) * 3
            if score > best_score:
                best_score = score
                best_idx = idx
        allocations[best_idx] += 1
        extra_slots -= 1

    subdivided_sections: list[dict[str, Any]] = []
    for idx, c in enumerate(narrative_plan):
        k = allocations[idx]
        if k == 1:
            subdivided_sections.append(dict(c))
            continue

        kp_list = list(c.get("key_points", []))
        eid_list = list(c.get("relevant_evidence_ids", []))
        base_heading = c.get("heading", f"Chapter {idx+1}")
        base_purpose = c.get("purpose", f"Explore {base_heading}")

        # Semantic candidate labels from evidence titles and key points
        sub_candidates: list[str] = []
        for eid in eid_list:
            if eid in items_by_id:
                it = items_by_id[eid]
                it_title = (it.get("title") or it.get("focus_area") or "").strip()
                if it_title:
                    clean_t = re.sub(r"^(?:source\s*\d+:?|section\s*\d+:?)\s*", "", it_title, flags=re.IGNORECASE).strip()
                    if clean_t and clean_t.lower() not in base_heading.lower() and clean_t not in sub_candidates:
                        sub_candidates.append(clean_t)

        if len(sub_candidates) < k and kp_list:
            for kp in kp_list:
                phrase = re.split(r"[:;,\.\-—]", kp)[0].strip()
                if phrase and 3 < len(phrase) < 40 and phrase.lower() not in base_heading.lower() and phrase not in sub_candidates:
                    sub_candidates.append(phrase)

        for j in range(k):
            # Key points slice
            if kp_list:
                s_kp = (j * len(kp_list)) // k
                e_kp = ((j + 1) * len(kp_list)) // k
                sub_kp = kp_list[s_kp:max(s_kp + 1, e_kp)]
            else:
                sub_kp = []

            # Evidence slice
            if eid_list:
                s_ev = (j * len(eid_list)) // k
                e_ev = ((j + 1) * len(eid_list)) // k
                sub_eids = eid_list[s_ev:max(s_ev + 1, e_ev)]
            else:
                sub_eids = []

            if j < len(sub_candidates):
                sub_label = sub_candidates[j]
                sub_heading = f"{base_heading} — {sub_label}"
                sub_purpose = f"{base_purpose} Focus specifically on {sub_label}."
            else:
                sub_heading = f"{base_heading} (Part {j+1})"
                sub_purpose = f"{base_purpose} (Part {j+1})."

            subdivided_sections.append({
                "heading": sub_heading,
                "purpose": sub_purpose,
                "key_points": sub_kp,
                "relevant_evidence_ids": sub_eids,
            })

    return subdivided_sections


def build_episode_outline(
    topic: str,
    evidence_packet: dict[str, Any],
    target_minutes: str | int | None,
    scope: EvidenceScope,
    source_ledger: dict[str, Any] | None = None,
    research_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build structured episode outline with topic/evidence-specific section headings,
    explicit target ranges, assigned key points, and anti-repetition guidance.

    Prioritizes model-generated topic-specific narrative plans from grounded research.
    Separates narrative structure from generation granularity (subdivides/merges deterministically).
    Retains domain archetypes ONLY as deterministic fallback when research plan is unavailable.
    Preserves semantic evidence associations over positional slicing.
    """
    target_budget = get_target_word_budget(target_minutes)
    is_auto = target_budget is None

    items = evidence_packet.get("items", [])
    items_by_id = {it["evidence_id"]: it for it in items if it.get("evidence_id")}
    evidence_ids = list(items_by_id.keys())

    source_words = len((source_ledger.get("clean_text", "") if source_ledger else "").split())

    # Source mode bounds check
    evidence_supported_target = target_budget
    if scope == EvidenceScope.SOURCE_ONLY and target_budget is not None and source_words > 0:
        max_legitimate_words = int(source_words * 2.0)
        if target_budget > max_legitimate_words:
            logger.info(
                f"Source mode: source word count ({source_words}) cannot legitimately support requested budget ({target_budget} words). "
                f"Bounding word budget to {max_legitimate_words} words."
            )
            evidence_supported_target = max_legitimate_words

    # 1. Determine target generation section count based on budget
    narrative_plan = evidence_packet.get("narrative_plan")
    use_narrative_plan = (
        scope != EvidenceScope.SOURCE_ONLY
        and isinstance(narrative_plan, list)
        and len(narrative_plan) >= 2
    )

    if is_auto:
        nominal_sec_count = max(2, min(len(narrative_plan) if narrative_plan else 5, 6))
    else:
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

    if use_narrative_plan:
        section_proposals = adapt_narrative_plan_to_generation_sections(
            narrative_plan=narrative_plan,
            target_count=nominal_sec_count,
            items_by_id=items_by_id,
        )
        sec_count = len(section_proposals)
    else:
        # Build proposals from source ledger, research focus areas, or items
        headings_pool: list[dict[str, Any]] = []
        if scope == EvidenceScope.SOURCE_ONLY and source_ledger and source_ledger.get("headings"):
            for h in source_ledger["headings"]:
                headings_pool.append({
                    "heading": h,
                    "purpose": f"Explore source content on: {h}",
                    "relevant_evidence_ids": [],
                    "key_points": [],
                })
        elif research_plan and research_plan.get("focus_areas"):
            for fa in research_plan["focus_areas"]:
                headings_pool.append({
                    "heading": fa["name"],
                    "purpose": fa.get("focus", f"Explore {fa['name']}"),
                    "relevant_evidence_ids": [],
                    "key_points": [],
                })
        elif items:
            for it in items:
                t = it.get("title") or it.get("focus_area")
                if t and not any(p["heading"] == t for p in headings_pool):
                    headings_pool.append({
                        "heading": t,
                        "purpose": f"Analyze key facts and findings regarding {t}.",
                        "relevant_evidence_ids": [it["evidence_id"]],
                        "key_points": [it.get("snippet", "")[:120]] if it.get("snippet") else [],
                    })

        # Fallback to domain templates ONLY if pool is empty
        if not headings_pool:
            domain_plan = build_research_plan(topic=topic, research_depth="high", scope=scope)
            for fa in domain_plan.get("focus_areas", []):
                headings_pool.append({
                    "heading": fa["name"],
                    "purpose": fa.get("focus", f"Explore {fa['name']}"),
                    "relevant_evidence_ids": [],
                    "key_points": [],
                })

        # Section counts and target word distribution for non-narrative-plan flow
        if is_auto:
            sec_count = max(2, min(len(headings_pool), 6))
        else:
            if evidence_ids:
                if scope == EvidenceScope.SOURCE_ONLY:
                    max_supported_secs = max(2, min(len(evidence_ids), len(headings_pool) or len(evidence_ids)))
                else:
                    max_supported_secs = max(2, min(len(evidence_ids) + 1, nominal_sec_count))
                sec_count = min(nominal_sec_count, max_supported_secs)
            else:
                sec_count = nominal_sec_count

        section_proposals = []
        for i in range(sec_count):
            if i < len(headings_pool):
                section_proposals.append(headings_pool[i])
            else:
                base = headings_pool[i % len(headings_pool)]
                section_proposals.append({
                    "heading": f"{base['heading']} (Part {i+1})",
                    "purpose": f"Deepen exploration of {base['heading']}.",
                    "relevant_evidence_ids": list(base.get("relevant_evidence_ids", [])),
                    "key_points": list(base.get("key_points", [])),
                })

    if is_auto:
        section_word_budget = None
        effective_total_words = None
    else:
        effective_total_words = evidence_supported_target
        section_word_budget = evidence_supported_target // sec_count

    # 2. Semantic Evidence Mapping
    # Validate proposed evidence IDs against items_by_id and assign based on thematic relevance
    section_evidence_assignments: list[list[str]] = []
    assigned_any_semantic = False

    for prop in section_proposals:
        matched_eids: list[str] = []
        # Validate explicit evidence IDs
        for eid in prop.get("relevant_evidence_ids", []):
            if eid in items_by_id and eid not in matched_eids:
                matched_eids.append(eid)

        # Match evidence by focus area or heading/purpose keyword overlap
        heading_words = {w.lower() for w in re.findall(r"\w{4,}", prop.get("heading", ""))}
        purpose_words = {w.lower() for w in re.findall(r"\w{4,}", prop.get("purpose", ""))}
        kw_set = heading_words | purpose_words

        for it in items:
            eid = it.get("evidence_id")
            if not eid or eid in matched_eids:
                continue
            fa = (it.get("focus_area") or "").lower()
            title = (it.get("title") or "").lower()
            snip = (it.get("snippet") or "")[:200].lower()
            if any(w in fa or w in title for w in kw_set if len(w) > 3):
                matched_eids.append(eid)
            elif sum(1 for w in kw_set if w in snip) >= 2:
                matched_eids.append(eid)

        if matched_eids:
            assigned_any_semantic = True
        section_evidence_assignments.append(matched_eids)

    # Positional slicing ONLY as fallback when no thematic association exists
    if not assigned_any_semantic and evidence_ids:
        for i in range(sec_count):
            if len(evidence_ids) <= sec_count:
                chunk_idx = (i * len(evidence_ids)) // sec_count
                section_evidence_assignments[i] = [evidence_ids[chunk_idx]]
            else:
                start_ev = (i * len(evidence_ids)) // sec_count
                end_ev = ((i + 1) * len(evidence_ids)) // sec_count
                section_evidence_assignments[i] = evidence_ids[start_ev:max(start_ev + 1, end_ev)]
    else:
        # Ensure no evidence is orphaned: assign unassigned items to best-matching section
        assigned_all_eids = {eid for sec_eids in section_evidence_assignments for eid in sec_eids}
        for it in items:
            eid = it.get("evidence_id")
            if eid and eid not in assigned_all_eids:
                # Find best section by keyword match, or section 0 if none
                best_sec_idx = 0
                max_score = 0
                it_text = f"{it.get('title', '')} {it.get('snippet', '')[:150]}".lower()
                for s_idx, prop in enumerate(section_proposals):
                    score = sum(1 for w in re.findall(r"\w{4,}", prop.get("heading", "").lower()) if w in it_text)
                    if score > max_score:
                        max_score = score
                        best_sec_idx = s_idx
                section_evidence_assignments[best_sec_idx].append(eid)
                assigned_all_eids.add(eid)

    # 3. Assemble sections
    sections = []
    for i, prop in enumerate(section_proposals):
        idx = i + 1
        heading = prop["heading"]
        purpose = prop["purpose"]
        assigned_ev = section_evidence_assignments[i] if i < len(section_evidence_assignments) else []

        # Extract compact key points from assigned evidence or proposal
        key_points = list(prop.get("key_points") or [])
        if not key_points:
            for eid in assigned_ev:
                if eid in items_by_id:
                    it = items_by_id[eid]
                    snip = it.get("snippet", "").strip()
                    if snip:
                        first_sent = re.split(r"(?<=[.!?])\s+", snip)[0].strip()
                        if first_sent and len(first_sent) > 10:
                            key_points.append(first_sent[:120])
        if not key_points and purpose:
            key_points.append(purpose[:120])

        w_min = int(round(section_word_budget * 0.85)) if section_word_budget else None
        w_max = int(round(section_word_budget * 1.15)) if section_word_budget else None

        anti_rep = (
            f"Focus directly on the specific narrative job of {heading.lower()}. "
            "Do not re-introduce the overall topic or repeat earlier section facts."
        )

        sections.append({
            "section_index": idx,
            "heading": heading,
            "purpose": purpose,
            "word_budget": section_word_budget,
            "word_budget_min": w_min,
            "word_budget_max": w_max,
            "relevant_evidence_ids": assigned_ev,
            "key_points": key_points[:3],
            "anti_repetition": anti_rep,
            "transition_intent": f"Flow naturally from section {idx-1}" if idx > 1 else "Opening hook",
        })

    return {
        "episode_title": topic,
        "episode_description": f"An in-depth exploration of {topic}.",
        "requested_target_words": get_target_word_budget(target_minutes),
        "target_total_words": effective_total_words,
        "section_count": len(sections),
        "sections": sections,
        "is_auto": is_auto,
    }


def generate_single_section(
    job: PodcastJob,
    section_info: dict[str, Any],
    topic: str,
    evidence_packet: dict[str, Any],
    previous_summary: str | None = None,
    scope: EvidenceScope = EvidenceScope.SOURCE_ONLY,
    db: Any = None,
    covered_context: str | None = None,
) -> dict[str, Any]:
    """
    Generate one section of the long-form podcast script grounded strictly in assigned evidence.
    Enforces a strict single-pass contract: exactly one model request per section.
    Places control instructions in trusted generation_instructions outside untrusted SOURCE_DATA.
    """
    sec_idx = section_info["section_index"]
    heading = section_info["heading"]
    purpose = section_info["purpose"]
    budget = section_info.get("word_budget")
    budget_min = section_info.get("word_budget_min") or (int(round(budget * 0.85)) if budget else None)
    budget_max = section_info.get("word_budget_max") or (int(round(budget * 1.15)) if budget else None)
    ev_ids = section_info.get("relevant_evidence_ids", [])
    key_points = section_info.get("key_points", [])
    anti_rep = section_info.get("anti_repetition", "")

    all_items = {it["evidence_id"]: it for it in evidence_packet.get("items", [])}
    assigned_snippets = []
    for eid in ev_ids:
        if eid in all_items:
            it = all_items[eid]
            assigned_snippets.append(f"[{it['evidence_id']} - {it['title']}]:\n{it['snippet']}")

    evidence_text = "\n\n".join(assigned_snippets) or f"Evidence regarding {topic}."

    anti_rep_context = covered_context or previous_summary
    if anti_rep_context:
        if "ALREADY COVERED" in anti_rep_context:
            prev_context = anti_rep_context
        else:
            prev_context = (
                f"Previous section covered: {anti_rep_context}. Do NOT repeat those introductory facts. Continue the narrative naturally with a smooth spoken transition."
            )
    else:
        prev_context = "This is the opening section. Hook the listener and state the central premise directly without meta-announcements."

    budget_instruction = (
        f"Target Word Range: approximately {budget_min}–{budget_max} words (nominal target: {budget} words). "
        "Prioritize conversational clarity, completeness, and natural spoken flow over exact word length."
        if budget is not None
        else "Write natural, comprehensive spoken narration covering the assigned evidence thoroughly without artificial brevity."
    )

    key_points_block = ""
    if key_points:
        key_points_block = "Key Points to Cover:\n" + "\n".join(f"- {kp}" for kp in key_points) + "\n\n"

    anti_rep_block = f"Anti-Repetition Guidance:\n{anti_rep}\n\n" if anti_rep else ""

    control_instructions = f"""You are writing Section {sec_idx} of a long-form podcast about: {topic}
Section Heading: {heading}
Purpose: {purpose}
{budget_instruction}

IMPORTANT: The JSON response is a standalone response for this single section only. Its segments array uses response-local numbering and MUST begin with order=1 (e.g. order=1, 2, ...), regardless of the logical podcast section number ({sec_idx}).

{prev_context}

{key_points_block}{anti_rep_block}Spoken-Writing Contract:
1. Narration must be written specifically for LISTENING, not an essay: use conversational, explanatory prose and natural spoken transitions.
2. Sentence Cadence & Rhythm: Varied sentence length is essential. Alternate short, punchy emphasis sentences (5–10 words) with medium explanatory sentences (15–25 words). Avoid monotonous, uniform pacing.
3. Natural Contractions: Use contractions (it's, they've, don't, wasn't, there's, we've) wherever natural for spoken English narration.
4. Rhetorical Questions & Transitions: Limit rhetorical questions to at most one per section. Avoid formulaic transition templates (never start a section with "In the previous section we explored..." or "Now let's turn our attention to..."). Advance directly into the narrative.
5. Explain technical jargon clearly before relying upon it. Avoid dense runs of unexpanded acronyms, statistics, dates, or mechanical lists.
6. Ground all factual assertions strictly in the provided evidence. Preserve uncertainty and attribution accurately.
7. NEVER mechanically announce section headings (e.g., do not say 'Section two: Technical Architecture').
8. NEVER include a miniature conclusion, moral, or recap at the end of this section.
9. Do not invent outside facts absent from the provided evidence.
10. Output schema constraint: Regardless of this section's global index ({sec_idx}), this output is an isolated section response. Its segments array uses response-local numbering and MUST begin with order=1.
"""

    t_sec0 = datetime.now(UTC)

    def _execute_section(p_inst: Any, attempt: int, src: str) -> PodcastScriptResponse:
        try:
            resp = p_inst.generate_script(
                source_text=src,
                request_mode="standard",
                source_title=topic,
                job_id=job.id,
                generation_instructions=control_instructions,
                is_isolated_section=True,
            )
        except TypeError as te:
            if "is_isolated_section" in str(te):
                resp = p_inst.generate_script(
                    source_text=src,
                    request_mode="standard",
                    source_title=topic,
                    job_id=job.id,
                    generation_instructions=control_instructions,
                )
            else:
                raise
        if isinstance(resp, dict):
            return parse_isolated_section_response(resp)
        return resp

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
    elapsed_sec = round((datetime.now(UTC) - t_sec0).total_seconds(), 2)

    return {
        "section_index": sec_idx,
        "heading": heading,
        "purpose": purpose,
        "narration": full_narration,
        "word_count": actual_words,
        "target_word_budget": budget,
        "target_word_range": (budget_min, budget_max) if budget else None,
        "relevant_evidence_ids": ev_ids,
        "generation_call_count": 1,
        "elapsed_seconds": elapsed_sec,
        "completed": True,
    }


def expand_single_section(
    job: PodcastJob,
    section_info: dict[str, Any],
    current_narration: str,
    actual_words: int,
    target_budget: int,
    topic: str,
    evidence_packet: dict[str, Any],
    scope: EvidenceScope,
    covered_context: str | None = None,
    db: Any = None,
) -> dict[str, Any]:
    """
    Perform a single controlled section expansion pass when generated words are
    below the effective minimum/target word budget AND grounded evidence remains available.
    Instructs the model to add new grounded value (mechanisms, consequences, context, examples)
    without restating or paraphrasing existing material.
    Uses normal provider failover; degrades gracefully if failover fails.
    """
    deficit = target_budget - actual_words
    sec_idx = section_info["section_index"]
    heading = section_info["heading"]
    purpose = section_info["purpose"]
    ev_ids = section_info.get("relevant_evidence_ids", [])

    all_items = {it.get("evidence_id"): it for it in evidence_packet.get("items", []) if it.get("evidence_id")}
    assigned_snippets = []
    for eid in ev_ids:
        if eid in all_items:
            it = all_items[eid]
            title_suffix = f" - {it['title']}" if it.get("title") else ""
            assigned_snippets.append(f"[{it.get('evidence_id', eid)}{title_suffix}]:\n{it.get('snippet', '')}")

    evidence_text = "\n\n".join(assigned_snippets) or f"Evidence regarding {topic}."

    source_only_rule = ""
    if scope == EvidenceScope.SOURCE_ONLY:
        source_only_rule = (
            "\n6. SOURCE-ONLY GROUNDING REQUIREMENT: Use ONLY the supplied source/evidence. "
            "Do NOT introduce outside facts, speculation, or external knowledge merely to reach duration. "
            "If the supplied source material cannot support additional grounded detail, "
            "gracefully retain the existing text without adding empty filler."
        )

    expansion_instructions = f"""You are expanding Section {sec_idx} of a long-form podcast about: {topic}
Section Heading: {heading}
Section Purpose: {purpose}
Current Draft Word Count: {actual_words} words. Target Budget: approximately {target_budget} words.
Remaining Word Deficit to Fulfill: approximately {deficit} words.

CURRENT DRAFT FOR THIS SECTION:
\"\"\"
{current_narration}
\"\"\"

EXPANSION CONTRACT:
1. Add NEW VALUE to fulfill the target budget through:
   - concrete mechanisms and operational details from the evidence
   - cause-and-effect consequences and implications
   - historical or contextual chronology
   - grounded illustrative examples
   - nuanced distinctions and caveats
2. DO NOT simply restate, summarize, or paraphrase material already in the current draft.
3. Write in seamless, natural spoken prose with varied sentence cadence and natural contractions.
4. Output schema: Provide a complete updated section response where the expansion is seamlessly integrated into the narration. Response-local numbering must begin with order=1.
5. All assertions must be strictly grounded in the provided evidence.{source_only_rule}
"""

    t0 = datetime.now(UTC)

    def _execute_expansion(p_inst: Any, attempt: int, src: str) -> PodcastScriptResponse:
        try:
            resp = p_inst.generate_script(
                source_text=src,
                request_mode="standard",
                source_title=topic,
                job_id=job.id,
                generation_instructions=expansion_instructions,
                is_isolated_section=True,
            )
        except TypeError as te:
            if "is_isolated_section" in str(te):
                resp = p_inst.generate_script(
                    source_text=src,
                    request_mode="standard",
                    source_title=topic,
                    job_id=job.id,
                    generation_instructions=expansion_instructions,
                )
            else:
                raise
        if isinstance(resp, dict):
            return parse_isolated_section_response(resp)
        return resp

    try:
        res: PodcastScriptResponse = execute_with_failover(
            job=job,
            operation="section_expansion",
            execute_fn=_execute_expansion,
            db=db,
            source_text=evidence_text,
        )
        expanded_narration = "\n\n".join(seg.narration for seg in res.segments).strip()
        expanded_words = len(expanded_narration.split())
        words_added = expanded_words - actual_words

        # Only accept if expansion actually added words and did not shrink or corrupt
        if expanded_words >= actual_words + 25:
            elapsed = round((datetime.now(UTC) - t0).total_seconds(), 2)
            logger.info(
                f"Section {sec_idx} expansion successful: {actual_words} -> {expanded_words} words "
                f"(+{words_added} words, target: {target_budget}) in {elapsed}s"
            )
            return {
                "success": True,
                "narration": expanded_narration,
                "word_count": expanded_words,
                "words_added": words_added,
                "elapsed_seconds": elapsed,
            }
        else:
            logger.info(
                f"Section {sec_idx} expansion produced insufficient words ({expanded_words} vs {actual_words}); retaining draft."
            )
            return {
                "success": False,
                "narration": current_narration,
                "word_count": actual_words,
                "words_added": 0,
                "reason": "expansion_below_threshold",
            }
    except Exception as e:
        logger.warning(f"Section {sec_idx} expansion failed non-fatally; retaining original draft: {e}")
        return {
            "success": False,
            "narration": current_narration,
            "word_count": actual_words,
            "words_added": 0,
            "reason": f"expansion_error: {e}",
        }


def _extract_bounded_excerpt(full_text: str, target: str, window_words: int = 40) -> str:
    """
    Extract a bounded window of words (~40 words / 1-2 sentences) around target in full_text.
    If target is not found or empty, returns the first 60 words.
    """
    if not full_text:
        return ""
    full_words = full_text.split()
    if not target or not target.strip():
        return " ".join(full_words[:min(len(full_words), window_words * 2)])

    target_tokens = [re.sub(r"[^\w\-]", "", w).lower() for w in target.split() if w]
    if not target_tokens:
        return " ".join(full_words[:min(len(full_words), window_words * 2)])

    # Search for target sequence in full_text
    clean_tokens = [re.sub(r"[^\w\-]", "", w).lower() for w in full_words]
    match_idx = -1
    t_len = len(target_tokens)
    for i in range(len(clean_tokens) - t_len + 1):
        if clean_tokens[i : i + t_len] == target_tokens:
            match_idx = i
            break

    if match_idx == -1:
        # Fallback to single token overlap
        for i, tok in enumerate(clean_tokens):
            if tok in target_tokens and len(tok) >= 4:
                match_idx = i
                break

    if match_idx != -1:
        start = max(0, match_idx - window_words // 2)
        end = min(len(full_words), match_idx + t_len + window_words // 2)
        prefix = "... " if start > 0 else ""
        suffix = " ..." if end < len(full_words) else ""
        return prefix + " ".join(full_words[start:end]) + suffix

    return " ".join(full_words[:min(len(full_words), window_words * 2)])


def review_script_repetition(
    job: PodcastJob,
    completed_sections: list[dict[str, Any]],
    near_duplicate_warnings: list[Any],
    distinctive_phrase_warnings: list[Any],
    topic: str,
    db: Any = None,
) -> tuple[RepetitionReviewResponse | None, list[dict[str, Any]], dict[str, Any]]:
    """
    Structured AI repetition review.
    Evaluates candidate duplicates and distinctive concepts with bounded actual text excerpts
    from BOTH Section A and Section B to distinguish repeated explanation from legitimate recurring terminology.
    Returns (review_response, candidate_warnings_to_repair, repetition_diagnostics_meta).
    """
    candidates = []
    # Collect candidate items from near duplicate warnings
    for w in near_duplicate_warnings:
        meta = w.metadata if hasattr(w, "metadata") else (w.get("metadata", {}) if isinstance(w, dict) else {})
        sec_a = meta.get("section_a")
        sec_b = meta.get("section_b")
        p_a = meta.get("passage_a") or ""
        p_b = meta.get("passage_b") or p_a or ""
        sim = meta.get("similarity", 0.0)
        if sec_a and sec_b:
            candidates.append({
                "section_a": sec_a,
                "section_b": sec_b,
                "candidate_text": p_b,
                "passage_a_seed": p_a,
                "is_near_dup": True,
                "similarity": float(sim),
                "repetition_count": 2,
                "phrase_length": len(p_b.split()),
                "is_named": False,
                "is_numeric": False,
            })

    # Collect candidate items from distinctive phrase warnings (if repeated across sections)
    for pw in distinctive_phrase_warnings:
        pmeta = pw.metadata if hasattr(pw, "metadata") else (pw.get("metadata", {}) if isinstance(pw, dict) else {})
        phrase = pmeta.get("phrase")
        secs = pmeta.get("sections") or []
        if phrase and len(secs) >= 2:
            words = phrase.split()
            rep_count = pmeta.get("repetition_count", len(secs))
            is_named = pmeta.get("is_named") if pmeta.get("is_named") is not None else any(w[0].isupper() for w in words if w)
            is_numeric = pmeta.get("is_numeric") if pmeta.get("is_numeric") is not None else any(ch.isdigit() for ch in phrase)
            sec_a = secs[0]
            for sec_b in secs[1:]:
                candidates.append({
                    "section_a": sec_a,
                    "section_b": sec_b,
                    "candidate_text": phrase,
                    "passage_a_seed": phrase,
                    "is_near_dup": False,
                    "similarity": 0.0,
                    "repetition_count": rep_count,
                    "phrase_length": len(words),
                    "is_named": is_named,
                    "is_numeric": is_numeric,
                })

    if not candidates:
        return None, [], {
            "initial_near_duplicate_warnings_count": len(near_duplicate_warnings),
            "distinctive_concept_candidates_count": len(distinctive_phrase_warnings),
            "distinctive_concept_candidates": [],
            "candidate_ranking_selection": [],
            "evaluated_count": 0,
            "total_candidate_count": 0,
            "omitted_candidates": [],
        }

    # Group candidates by section pair to collapse overlapping phrase variants
    candidates_by_pair: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for c in candidates:
        candidates_by_pair.setdefault((c["section_a"], c["section_b"]), []).append(c)

    collapsed_candidates: list[dict[str, Any]] = []
    for pair, pair_cands in candidates_by_pair.items():
        near_dups = [c for c in pair_cands if c["is_near_dup"]]
        phrase_cands = [c for c in pair_cands if not c["is_near_dup"]]

        collapsed_candidates.extend(near_dups)

        # Collapse overlapping phrase variants within the pair
        phrase_cands.sort(key=lambda c: len(c["candidate_text"].split()), reverse=True)
        accepted_phrases: list[dict[str, Any]] = []
        for pc in phrase_cands:
            txt_lower = pc["candidate_text"].strip().lower()
            pc_words = set(re.sub(r"[^\w\-]", "", w) for w in txt_lower.split() if w)
            merged = False
            for acc in accepted_phrases:
                acc_lower = acc["candidate_text"].strip().lower()
                acc_words = set(re.sub(r"[^\w\-]", "", w) for w in acc_lower.split() if w)

                is_subset = bool(pc_words and acc_words and (pc_words.issubset(acc_words) or acc_words.issubset(pc_words)))

                if is_subset:
                    acc["repetition_count"] = max(acc.get("repetition_count", 2), pc.get("repetition_count", 2))
                    acc["is_named"] = acc.get("is_named", False) or pc.get("is_named", False)
                    acc["is_numeric"] = acc.get("is_numeric", False) or pc.get("is_numeric", False)
                    if len(pc["candidate_text"].split()) > len(acc["candidate_text"].split()) or (pc.get("is_named") and not acc.get("is_named")):
                        acc["candidate_text"] = pc["candidate_text"]
                        acc["passage_a_seed"] = pc["passage_a_seed"]
                    merged = True
                    break
            if not merged:
                accepted_phrases.append(pc)

        collapsed_candidates.extend(accepted_phrases)

    # Prioritize candidates deterministically
    def _candidate_sort_key(c: dict[str, Any]) -> tuple:
        is_nd = c["is_near_dup"]
        sim = float(c.get("similarity", 0.0))
        is_named = bool(c.get("is_named"))
        is_numeric = bool(c.get("is_numeric"))
        words = [re.sub(r"[^\w\-]", "", w.lower()) for w in c["candidate_text"].split()]
        all_substantive = len(words) >= 2 and all(len(w) >= 4 and w not in COMMON_PHRASE_STOPWORDS for w in words)
        tier = 0
        if is_named and is_numeric:
            tier = 3
        elif is_named or is_numeric:
            tier = 2
        elif all_substantive:
            tier = 1

        rep_count = int(c.get("repetition_count", 2))
        phrase_len = int(c.get("phrase_length", len(words)))

        return (
            not is_nd,
            -sim if is_nd else 0.0,
            -tier,
            -rep_count,
            -phrase_len,
            c["section_a"],
            c["section_b"],
            c["candidate_text"].lower(),
        )

    collapsed_candidates.sort(key=_candidate_sort_key)

    # Configurable review candidate cap (default 14, bounds 12-16)
    configured_cap = getattr(settings, "HERALD_MAX_REPETITION_REVIEW_CANDIDATES", 14)
    max_review_candidates = max(12, min(16, configured_cap))

    evaluated_candidates = collapsed_candidates[:max_review_candidates]
    omitted_candidates = []
    for c in collapsed_candidates[max_review_candidates:]:
        omitted_candidates.append({
            "section_a": c["section_a"],
            "section_b": c["section_b"],
            "candidate_text": c["candidate_text"],
            "reason": f"exceeded_candidate_cap_{max_review_candidates}",
        })

    if omitted_candidates:
        logger.info(
            f"Repetition review capped at {max_review_candidates} candidates; "
            f"recording {len(omitted_candidates)} omitted candidates in diagnostics."
        )

    logger.info(f"Running structured repetition review for job {job.id} on {len(evaluated_candidates)} candidates.")

    # Format candidates for review prompt with bounded actual text excerpts from BOTH sections
    candidate_prompts = []
    for idx, c in enumerate(evaluated_candidates, 1):
        s_a = c["section_a"]
        s_b = c["section_b"]
        txt = c["candidate_text"]
        p_a_seed = c.get("passage_a_seed", "")
        sec_a_data = completed_sections[s_a - 1] if 1 <= s_a <= len(completed_sections) else {}
        sec_b_data = completed_sections[s_b - 1] if 1 <= s_b <= len(completed_sections) else {}
        h_a = sec_a_data.get("heading", f"Section {s_a}")
        h_b = sec_b_data.get("heading", f"Section {s_b}")
        narr_a = sec_a_data.get("narration", "")
        narr_b = sec_b_data.get("narration", "")

        excerpt_a = _extract_bounded_excerpt(narr_a, p_a_seed or txt)
        excerpt_b = _extract_bounded_excerpt(narr_b, txt)
        c["excerpt_a"] = excerpt_a
        c["excerpt_b"] = excerpt_b

        candidate_prompts.append(
            f"Candidate {idx}:\n"
            f"- Earlier Section {s_a} ('{h_a}') vs Later Section {s_b} ('{h_b}')\n"
            f"- Concept / Phrase under evaluation: \"{txt}\"\n"
            f"  * Section {s_a} Excerpt: \"{excerpt_a}\"\n"
            f"  * Section {s_b} Excerpt: \"{excerpt_b}\""
        )

    review_instructions = f"""You are evaluating potential narrative repetition in a podcast script about: {topic}

CANDIDATES IDENTIFIED BY HEURISTIC FILTERS (WITH ACTUAL EXCERPTS FROM BOTH SECTIONS):
{chr(10).join(candidate_prompts)}

EVALUATION RULES:
1. Carefully compare the actual excerpt from Section A with the actual excerpt from Section B.
2. Distinguish between substantive repetition (repeating an explanation, backstory, or key point that was already explained in Section A) vs legitimate recurring terminology (e.g. specialized terms, acronyms, thematic callbacks, names, or metrics).
3. Legitimate recurring terminology is ALLOWED and must have is_substantive_repetition=false.
4. If Section B redundantly re-explains, recaps, or duplicates what Section A already explained, mark is_substantive_repetition=true, provide the specific passage_to_repair from Section B, and set passage_a to the corresponding explanation from Section A.
5. Output a JSON object adhering strictly to the RepetitionReviewResponse schema:
   {{
     "has_substantive_repetition": boolean,
     "reviews": [
       {{
         "section_b": integer,
         "section_a": integer,
         "concept_or_passage": string,
         "is_substantive_repetition": boolean,
         "explanation": string,
         "passage_to_repair": string or null,
         "passage_a": string or null
       }}
     ]
   }}
"""

    def _exec_rep_review(p_inst: Any, attempt: int, src: str) -> RepetitionReviewResponse:
        resp = p_inst.generate_structured_output(
            prompt=review_instructions,
            response_schema=RepetitionReviewResponse,
            job_id=job.id,
            operation="repetition_review",
            attempt=attempt,
        )
        if isinstance(resp, dict):
            return RepetitionReviewResponse(**resp)
        return resp

    review_res = None
    try:
        review_res = execute_with_failover(
            job=job,
            operation="repetition_review",
            execute_fn=_exec_rep_review,
            db=db,
            source_text=topic,
            required_capability="structured_output",
        )
    except Exception as e:
        logger.warning(f"Structured repetition review failed non-fatally; falling back to heuristic warnings: {e}")

    # Build confirmed list of warnings to repair, preserving earlier section context (passage_a)
    to_repair = []
    # Build lookup from evaluated_candidates for excerpt_a fallback
    cand_lookup = {(c["section_a"], c["section_b"]): c for c in evaluated_candidates}
    if review_res and review_res.has_substantive_repetition:
        for r in review_res.reviews:
            if r.is_substantive_repetition:
                c_info = cand_lookup.get((r.section_a, r.section_b), {})
                p_a = r.passage_a or c_info.get("excerpt_a") or ""
                to_repair.append({
                    "metadata": {
                        "section_a": r.section_a,
                        "section_b": r.section_b,
                        "passage_a": p_a,
                        "passage_b": r.passage_to_repair or r.concept_or_passage,
                        "concept": r.concept_or_passage,
                        "explanation": r.explanation,
                    }
                })
    elif review_res is None:
        # Fallback to original near-duplicate warnings if review call failed
        for w in near_duplicate_warnings:
            meta = w.metadata if hasattr(w, "metadata") else (w.get("metadata", {}) if isinstance(w, dict) else {})
            to_repair.append({"metadata": dict(meta)})

    rep_meta = {
        "initial_near_duplicate_warnings_count": len(near_duplicate_warnings),
        "distinctive_concept_candidates_count": len(distinctive_phrase_warnings),
        "distinctive_concept_candidates": [
            (pw.metadata.get("phrase") if hasattr(pw, "metadata") else pw.get("metadata", {}).get("phrase"))
            for pw in distinctive_phrase_warnings
            if (hasattr(pw, "metadata") and pw.metadata.get("phrase")) or (isinstance(pw, dict) and pw.get("metadata", {}).get("phrase"))
        ],
        "candidate_ranking_selection": [
            {
                "section_a": c["section_a"],
                "section_b": c["section_b"],
                "candidate_text": c["candidate_text"],
                "is_near_dup": c["is_near_dup"],
                "is_named": c.get("is_named", False),
                "is_numeric": c.get("is_numeric", False),
                "repetition_count": c.get("repetition_count", 2),
            }
            for c in evaluated_candidates
        ],
        "evaluated_count": len(evaluated_candidates),
        "total_candidate_count": len(candidates),
        "omitted_candidates": omitted_candidates,
    }
    return review_res, to_repair, rep_meta


def repair_script_duplicates(
    job: PodcastJob,
    sections: list[dict[str, Any]],
    duplicate_warnings: list[Any],
    evidence_packet: dict[str, Any],
    topic: str,
    scope: EvidenceScope = EvidenceScope.SOURCE_ONLY,
    db: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Pre-TTS bounded duplicate repair pass.
    Receives only offending passages and neighboring context.
    Asks AI to replace redundant material with new grounded material or condense.
    Bounded to max 1 repair pass.
    """
    if not duplicate_warnings:
        return sections, {
            "repair_attempted": False,
            "repaired_count": 0,
            "repaired_section_indices": [],
            "repaired_section_word_counts": [],
            "repair_success": False,
        }

    logger.info(f"Triggering pre-TTS duplicate repair for job {job.id} on {len(duplicate_warnings)} duplicate warnings.")
    repaired_sections = [dict(s) for s in sections]
    repaired_count = 0
    repaired_section_indices = []
    repaired_section_word_counts = []

    sec_to_dups: dict[int, list[dict[str, Any]]] = {}
    for w in duplicate_warnings:
        meta = w.metadata if hasattr(w, "metadata") else (w.get("metadata", {}) if isinstance(w, dict) else {})
        sec_b = meta.get("section_b")
        if sec_b:
            sec_to_dups.setdefault(sec_b, []).append(meta)

    for sec_idx, dups in sec_to_dups.items():
        if sec_idx < 1 or sec_idx > len(repaired_sections):
            continue
        sec_data = repaired_sections[sec_idx - 1]
        orig_narr = sec_data.get("narration", "")

        redundant_snippets = [d.get("passage_b", "") for d in dups if d.get("passage_b")]
        if not redundant_snippets:
            continue

        # Extract context of earlier sections mentioned in dups, explicitly pairing passage_a and passage_b
        earlier_contexts = []
        redundant_entries = []
        for d in dups:
            sec_a_idx = d.get("section_a")
            p_a = d.get("passage_a") or ""
            p_b = d.get("passage_b") or ""
            conc = d.get("concept") or ""
            expl = d.get("explanation") or ""

            if sec_a_idx and 1 <= sec_a_idx <= len(repaired_sections):
                a_data = repaired_sections[sec_a_idx - 1]
                h_a = a_data.get("heading", f"Section {sec_a_idx}")
                item_str = f"- From Earlier Section {sec_a_idx} ('{h_a}'):\n  Earlier Covered Material: \"{p_a or '(Earlier section explanation)'}\""
                if expl:
                    item_str += f"\n  Reason repetitive: {expl}"
                earlier_contexts.append(item_str)

            entry_label = f'- Redundant passage in Section {sec_idx}: "{p_b}"'
            if conc and conc != p_b:
                entry_label += f' (Concept: "{conc}")'
            redundant_entries.append(entry_label)

        earlier_context_str = "\n\n".join(earlier_contexts) if earlier_contexts else "Earlier sections in the episode."
        redundant_display_str = "\n".join(redundant_entries) if redundant_entries else "\n".join(f'- "{snip}"' for snip in redundant_snippets)

        # Scoped evidence: use evidence items assigned to section B or relevant items
        all_packet_items = evidence_packet.get("items", []) if evidence_packet else []
        sec_eids = sec_data.get("relevant_evidence_ids", [])
        scoped_items = [it for it in all_packet_items if it.get("evidence_id") in sec_eids]
        if not scoped_items:
            scoped_items = all_packet_items[:4]

        scoped_snippets = [
            f"[{it.get('evidence_id', 'EV')}]: {it.get('snippet', '')}"
            for it in scoped_items if it.get("snippet")
        ]
        scoped_evidence_str = "\n".join(scoped_snippets) if scoped_snippets else "No additional evidence snippets."

        source_grounding_block = ""
        if scope == EvidenceScope.SOURCE_ONLY:
            source_grounding_block = """
SOURCE-ONLY GROUNDING REQUIREMENT:
You are operating in SOURCE-ONLY mode. Replacement material may ONLY use the supplied source/evidence. You MUST NOT introduce external facts, general knowledge not present in the source, or hallucinated details. If no further details can be extracted from the source evidence, cleanly condense or prune the repetitive sentences without padding.
"""

        prompt = f"""You are repairing a duplicate passage in Section {sec_idx} of a podcast about: {topic}
Section {sec_idx} Heading: {sec_data.get('heading', '')}
Section {sec_idx} Purpose: {sec_data.get('purpose', '')}
{source_grounding_block}
EARLIER COVERED MATERIAL (Already explained to listeners in earlier sections — DO NOT REPEAT):
{earlier_context_str}

REDUNDANT PASSAGES IDENTIFIED IN SECTION {sec_idx} (To be replaced, condensed, or pruned):
{redundant_display_str}

AVAILABLE SCOPED EVIDENCE FOR THIS SECTION:
{scoped_evidence_str}

CURRENT SECTION {sec_idx} NARRATION:
\"\"\"
{orig_narr}
\"\"\"

REPAIR CONTRACT:
1. Replace or condense the redundant material so Section {sec_idx} does NOT repeat facts or explanations from earlier sections.
2. If new grounded details from the scoped evidence are available, introduce fresh relevant details. If not, cleanly condense or prune the repetitive sentences.
3. Preserve the natural spoken flow, narrative rhythm, and tone of the rest of the section.
4. Output the complete, revised narration for Section {sec_idx} only. Response-local numbering must begin with order=1.
"""

        def _exec_dup_repair(p_inst: Any, attempt: int, src: str) -> PodcastScriptResponse:
            try:
                resp = p_inst.generate_script(
                    source_text=src,
                    request_mode="standard",
                    source_title=topic,
                    job_id=job.id,
                    generation_instructions=prompt,
                    is_isolated_section=True,
                    operation="duplicate_repair",
                    attempt=attempt,
                )
            except TypeError as te:
                if "is_isolated_section" in str(te) or "operation" in str(te):
                    resp = p_inst.generate_script(
                        source_text=src,
                        request_mode="standard",
                        source_title=topic,
                        job_id=job.id,
                        generation_instructions=prompt,
                    )
                else:
                    raise
            if isinstance(resp, dict):
                return parse_isolated_section_response(resp)
            return resp

        try:
            res: PodcastScriptResponse = execute_with_failover(
                job=job,
                operation="duplicate_repair",
                execute_fn=_exec_dup_repair,
                db=db,
                source_text=scoped_evidence_str,
            )
            repaired_narr = "\n\n".join(seg.narration for seg in res.segments).strip()
            if repaired_narr and len(repaired_narr.split()) >= 10:
                before_w = sec_data.get("word_count") or len(orig_narr.split())
                after_w = len(repaired_narr.split())
                sec_data["narration"] = repaired_narr
                sec_data["word_count"] = after_w
                repaired_count += 1
                repaired_section_indices.append(sec_idx)
                repaired_section_word_counts.append({
                    "section_index": sec_idx,
                    "before_words": before_w,
                    "after_words": after_w,
                })
        except Exception as e:
            logger.warning(f"Duplicate repair for section {sec_idx} failed non-fatally: {e}")

    return repaired_sections, {
        "repair_attempted": True,
        "repaired_count": repaired_count,
        "original_warnings_count": len(duplicate_warnings),
        "repaired_section_indices": repaired_section_indices,
        "repaired_section_word_counts": repaired_section_word_counts,
        "repair_success": repaired_count > 0,
    }


def cleanup_script_metadata(
    job: PodcastJob,
    script_dict: dict[str, Any],
    topic: str,
    scope: EvidenceScope = EvidenceScope.SOURCE_ONLY,
    db: Any = None,
    return_metadata: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
    """
    Perform a single bounded AI metadata cleanup pass on episode title and section headings ONLY.
    Never alters or regenerates script narration.
    Supports 'as of <date>' when topic is genuinely time-sensitive, using the job creation date.
    """
    meta = {
        "attempted": False,
        "performed": False,
        "failed": False,
        "skipped": False,
        "error": None,
    }
    curr_title = script_dict.get("episode_title", topic)
    segments = script_dict.get("segments", [])
    if not segments:
        meta["skipped"] = True
        meta["reason"] = "no_segments"
        return (script_dict, meta) if return_metadata else script_dict

    job_dt = getattr(job, "created_at", None) or datetime.now(UTC)
    date_str = job_dt.strftime("%B %Y")

    headings_summary = "\n".join(
        f"- Section {s.get('order', idx)}: {s.get('heading', '')}"
        for idx, s in enumerate(segments, 1)
    )

    source_grounding_block = ""
    if scope == EvidenceScope.SOURCE_ONLY:
        source_grounding_block = "\n5. Grounding: Refinements must strictly reflect the topic and source headings. Do NOT introduce factual claims, outside entities, or assertions not supported by the source."

    # If custom title was provided, preserve it strictly
    has_custom_title = bool(getattr(job, "custom_title", None) and str(job.custom_title).strip())
    if has_custom_title:
        title_directive = f"1. Episode Title: The user specified custom title '{job.custom_title.strip()}'. Preserve this title exactly."
    else:
        title_directive = (
            f"1. Episode Title: Generate a concise, natural, and compelling podcast episode title (3-8 words). "
            f"Do NOT mechanically copy or truncate the raw research query or search prompt. "
            f"If the topic is a rapidly evolving comparison or current state (e.g. comparing frontier AI models), "
            f"you may append 'as of {date_str}' if helpful. Do NOT add dates to historical, evergreen, or general scientific topics."
        )

    instructions = f"""You are refining the title and section headings for a podcast about: {topic}
Reference Date: {date_str}

Current Episode Title: {curr_title}
Current Headings:
{headings_summary}

METADATA CONTRACT:
{title_directive}
2. Section Headings: Replace generic continuation labels (such as 'Part 2' or 'Reading Part 2') with distinct, topic-focused headings that describe the specific narrative content of each section.
3. Remove trailing dangling punctuation (dashes, colons, commas).
4. Narration: Do NOT modify script narration; this pass only refines titles and headings.{source_grounding_block}
5. Return a JSON object matching MetadataCleanupResponse:
   {{
     "episode_title": string,
     "headings": [
       {{"order": integer, "heading": string}}
     ]
   }}
"""

    def _exec_meta(p_inst: Any, attempt: int, src: str) -> MetadataCleanupResponse:
        if hasattr(p_inst, "generate_structured_output"):
            resp = p_inst.generate_structured_output(
                prompt=instructions,
                response_schema=MetadataCleanupResponse,
                job_id=job.id,
                operation="metadata_cleanup",
                attempt=attempt,
            )
        else:
            resp = p_inst.generate_script(
                source_text=src,
                request_mode="brief",
                source_title=topic,
                job_id=job.id,
                generation_instructions=instructions,
                operation="metadata_cleanup",
                attempt=attempt,
            )
        if isinstance(resp, dict):
            return MetadataCleanupResponse(**resp)
        if hasattr(resp, "episode_title"):
            if isinstance(resp, MetadataCleanupResponse):
                return resp
            head_list = [
                {"order": seg.order, "heading": seg.heading}
                for seg in getattr(resp, "segments", [])
            ]
            return MetadataCleanupResponse(
                episode_title=getattr(resp, "episode_title", None),
                headings=head_list,
            )
        return resp

    meta["attempted"] = True
    try:
        res: MetadataCleanupResponse = execute_with_failover(
            job=job,
            operation="metadata_cleanup",
            execute_fn=_exec_meta,
            db=db,
            source_text=topic,
            required_capability="structured_output",
        )
        updated = False
        res_title = getattr(res, "episode_title", None) if not isinstance(res, dict) else res.get("episode_title")
        if not has_custom_title and res_title and len(str(res_title).strip()) > 3:
            script_dict["episode_title"] = str(res_title).strip()
            updated = True
        elif has_custom_title:
            script_dict["episode_title"] = job.custom_title.strip()

        headings_list = getattr(res, "headings", None) or getattr(res, "segments", []) if not isinstance(res, dict) else (res.get("headings") or res.get("segments") or [])
        if headings_list:
            for idx, seg in enumerate(script_dict.get("segments", [])):
                if idx < len(headings_list):
                    h_item = headings_list[idx]
                    h_val = getattr(h_item, "heading", None) if hasattr(h_item, "heading") else h_item.get("heading")
                    if h_val and str(h_val).strip():
                        seg["heading"] = str(h_val).strip()
                        updated = True

        meta["performed"] = updated
        if not updated:
            meta["skipped"] = True
            meta["reason"] = "no_changes_needed"
    except Exception as e:
        logger.warning(f"Metadata cleanup failed non-fatally: {e}")
        meta["failed"] = True
        meta["error"] = str(e)

    return (script_dict, meta) if return_metadata else script_dict



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

    # Scoped research evidence optimization: scope evidence items to assigned chunk/evidence IDs
    assigned_evidence_ids = {
        ev_id
        for sec in sections
        for ev_id in sec.get("relevant_evidence_ids", [])
        if ev_id
    }
    all_packet_items = evidence_packet.get("items", []) if evidence_packet else []
    if assigned_evidence_ids and all_packet_items:
        scoped_items = [it for it in all_packet_items if it.get("evidence_id") in assigned_evidence_ids]
        if not scoped_items:
            scoped_items = all_packet_items
    else:
        scoped_items = all_packet_items

    compact_items = [
        {
            "evidence_id": it.get("evidence_id", f"E{idx}"),
            "title": it.get("title", ""),
            "snippet": (it.get("snippet", "") or "")[:400],
            "source_url": it.get("source_url") or it.get("url") or "",
        }
        for idx, it in enumerate(scoped_items, 1)
    ]
    dossier_data = {
        "topic": (evidence_packet.get("topic") if evidence_packet else None) or job.custom_title or "Episode",
        "scope": scope.value if hasattr(scope, "value") else str(scope),
        "items": compact_items,
        "section_plan": [
            {"order": s.get("section_index", idx), "heading": s.get("heading", ""), "purpose": s.get("purpose", "")}
            for idx, s in enumerate(sections, 1)
        ],
    }

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

    def _run_semantic_audit(curr_script: dict[str, Any]) -> tuple[bool, str, dict[str, Any], int, int, int]:
        findings: dict[str, Any] = {}
        issues_detected = False
        repair_instructions_parts: list[str] = []
        expected_audits = 0
        completed_audits = 0
        failed_audits = 0

        if scope == EvidenceScope.SOURCE_ONLY:
            expected_audits += 1
            try:
                def _audit_source_fn(p_inst: Any, attempt: int, src: str) -> Any:
                    return p_inst.audit_script_fidelity(
                        source_text=src,
                        script_dict=curr_script,
                        job_id=job.id,
                        operation="verification",
                        attempt=attempt,
                    )

                res = execute_with_failover(
                    job=job,
                    operation="verification",
                    execute_fn=_audit_source_fn,
                    db=db,
                    source_text=primary_source_text,
                    required_capability="verification",
                )
                completed_audits += 1
                if hasattr(res, "has_material_issues") and res.has_material_issues:
                    issues_detected = True
                    if getattr(res, "repair_instructions", None):
                        repair_instructions_parts.append(res.repair_instructions)
                findings["source_audit"] = res.model_dump() if hasattr(res, "model_dump") else str(res)
            except Exception as e:
                failed_audits += 1
                logger.warning(f"Semantic source fidelity audit skipped/failed non-fatally: {e}")

        elif scope == EvidenceScope.SOURCE_PLUS_RESEARCH:
            # Expanded mode: Perform BOTH seed-source audit AND research/evidence support audit
            if primary_source_text:
                expected_audits += 1
                try:
                    def _audit_source_fn(p_inst: Any, attempt: int, src: str) -> Any:
                        return p_inst.audit_script_fidelity(
                            source_text=src,
                            script_dict=curr_script,
                            job_id=job.id,
                            operation="verification",
                            attempt=attempt,
                        )

                    res_s = execute_with_failover(
                        job=job,
                        operation="verification",
                        execute_fn=_audit_source_fn,
                        db=db,
                        source_text=primary_source_text,
                        required_capability="verification",
                    )
                    completed_audits += 1
                    if hasattr(res_s, "has_material_issues") and res_s.has_material_issues:
                        issues_detected = True
                        if getattr(res_s, "repair_instructions", None):
                            repair_instructions_parts.append(f"Source fidelity: {res_s.repair_instructions}")
                    findings["source_audit"] = res_s.model_dump() if hasattr(res_s, "model_dump") else str(res_s)
                except Exception as e:
                    failed_audits += 1
                    logger.warning(f"Semantic source fidelity audit in expanded mode skipped/failed: {e}")

            expected_audits += 1
            try:
                def _audit_res_fn(p_inst: Any, attempt: int, src: str) -> Any:
                    return p_inst.audit_research_script(
                        source_text=src,
                        research_dossier=dossier_data,
                        script_dict=curr_script,
                        job_id=job.id,
                        operation="research_audit",
                        attempt=attempt,
                    )

                res_r = execute_with_failover(
                    job=job,
                    operation="research_audit",
                    execute_fn=_audit_res_fn,
                    db=db,
                    source_text=primary_source_text or "Topic research",
                    required_capability="verification",
                )
                completed_audits += 1
                if hasattr(res_r, "has_material_issues") and res_r.has_material_issues:
                    issues_detected = True
                    if getattr(res_r, "repair_instructions", None):
                        repair_instructions_parts.append(f"Research fidelity: {res_r.repair_instructions}")
                findings["research_audit"] = res_r.model_dump() if hasattr(res_r, "model_dump") else str(res_r)
            except Exception as e:
                failed_audits += 1
                logger.warning(f"Semantic research support audit in expanded mode skipped/failed: {e}")

        elif scope == EvidenceScope.RESEARCH:
            # Topic mode: Perform research/evidence support audit
            expected_audits += 1
            try:
                def _audit_topic_fn(p_inst: Any, attempt: int, src: str) -> Any:
                    return p_inst.audit_research_script(
                        source_text=src,
                        research_dossier=dossier_data,
                        script_dict=curr_script,
                        job_id=job.id,
                        operation="research_audit",
                        attempt=attempt,
                    )

                res_t = execute_with_failover(
                    job=job,
                    operation="research_audit",
                    execute_fn=_audit_topic_fn,
                    db=db,
                    source_text=primary_source_text or "Topic research",
                    required_capability="verification",
                )
                completed_audits += 1
                if hasattr(res_t, "has_material_issues") and res_t.has_material_issues:
                    issues_detected = True
                    if getattr(res_t, "repair_instructions", None):
                        repair_instructions_parts.append(res_t.repair_instructions)
                findings["research_audit"] = res_t.model_dump() if hasattr(res_t, "model_dump") else str(res_t)
            except Exception as e:
                failed_audits += 1
                logger.warning(f"Semantic topic research audit skipped/failed non-fatally: {e}")

        return issues_detected, " ".join(repair_instructions_parts), findings, expected_audits, completed_audits, failed_audits

    # 1. Initial Authoritative Semantic Audit
    current_script_dict = _build_script_dict(sections)
    has_material_issues, repair_instructions, audit_findings, expected_audits, completed_audits, failed_audits = _run_semantic_audit(current_script_dict)

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

        affected_headings = [
            s.get("heading")
            for s in sections
            if s.get("heading") and any(
                term in s.get("narration", "").lower()
                for term in (omitted_numbers[:4] + omitted_entities[:4])
            )
        ]
        audit_payload = {
            "has_material_issues": True,
            "repair_instructions": repair_instructions or "Restore omitted material facts and correct factual inaccuracies.",
            "omitted_numbers": omitted_numbers[:8],
            "omitted_entities": omitted_entities[:8],
            "affected_segments": affected_headings or [s.get("heading") for s in sections[:2] if s.get("heading")],
        }

        try:
            if scope == EvidenceScope.SOURCE_ONLY:
                def _do_repair_src(p_inst: Any, att: int, src: str) -> PodcastScriptResponse:
                    resp = p_inst.repair_script_fidelity(
                        source_text=src,
                        script_dict=current_script_dict,
                        audit_result=audit_payload,
                        job_id=job.id,
                        operation="verification_repair",
                        attempt=att,
                    )
                    if isinstance(resp, dict):
                        resp = PodcastScriptResponse(**resp)
                    if not isinstance(resp, PodcastScriptResponse) or not resp.segments:
                        raise ValueError("Fidelity repair response returned invalid schema or empty segments")
                    return resp

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
                    resp = p_inst.repair_research_script(
                        source_text=src,
                        research_dossier=dossier_data,
                        script_dict=current_script_dict,
                        audit_result=audit_payload,
                        job_id=job.id,
                        operation="research_repair",
                        attempt=att,
                    )
                    if isinstance(resp, dict):
                        resp = PodcastScriptResponse(**resp)
                    if not isinstance(resp, PodcastScriptResponse) or not resp.segments:
                        raise ValueError("Fidelity repair response returned invalid schema or empty segments")
                    return resp

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
            re_issues, _, re_findings, re_exp, re_comp, re_failed = _run_semantic_audit(repaired_script_dict)
            audit_findings["final_re_audit"] = re_findings
            if not re_issues and re_comp == re_exp and re_failed == 0 and re_exp > 0:
                repair_succeeded = True
                audit_status = "repair_succeeded"
                unresolved_issue = False
            else:
                repair_succeeded = False
                audit_status = "unresolved_issue_remains"
                unresolved_issue = True
        else:
            audit_status = "unresolved_issue_remains"
            unresolved_issue = True
    elif repair_attempted:
        audit_status = "repair_attempted"
    elif has_material_issues:
        audit_status = "issue_detected"
    elif failed_audits > 0:
        audit_status = "failed_nonfatal"
    elif completed_audits == expected_audits and expected_audits > 0 and not has_material_issues:
        audit_status = "clean"
    else:
        audit_status = "skipped"

    has_content_warning = bool(unresolved_issue or (audit_status == "unresolved_issue_remains"))
    audit_result = {
        "status": audit_status,
        "audit_executed": completed_audits > 0,
        "expected_audits": expected_audits,
        "completed_audits": completed_audits,
        "failed_audits": failed_audits,
        "has_material_issues": has_material_issues,
        "repair_instructions": repair_instructions,
        "repair_attempted": repair_attempted,
        "repair_succeeded": repair_succeeded,
        "unresolved_issue": unresolved_issue,
        "has_unresolved_material_issues": bool(unresolved_issue or (audit_status == "unresolved_issue_remains")),
        "content_warning": has_content_warning,
        "fidelity_blocked": bool(unresolved_issue or (audit_status == "unresolved_issue_remains") or (has_material_issues and not repair_succeeded)),
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


def _set_script_substage(job: PodcastJob, substage: str, db: Any = None):
    cfg = dict(job.configuration_state_json or {})
    cfg["script_substage"] = substage
    if substage == "complete":
        cfg["script_current_section"] = None
    job.configuration_state_json = cfg
    if db:
        try:
            db.commit()
        except Exception:
            pass


def detect_content_gap(
    completed_sections: list[dict[str, Any]],
    planned_target: int | None,
    outline: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Detect whether script has a genuine content gap using Herald's centralized underfill tolerances.
    Evaluates both overall script underfill (< 80% tolerance) and individual section underfill (< 85% budget).
    Returns gap info dict if underfilled, or None if acceptable.
    """
    if not planned_target or planned_target <= 500:
        return None
    total_words = sum(s.get("word_count", 0) for s in completed_sections)
    fill_ratio = total_words / float(planned_target)
    underfill_tolerance = getattr(settings, "HERALD_TOTAL_UNDERFILL_TOLERANCE_RATIO", 0.80)
    section_min_ratio = getattr(settings, "HERALD_SECTION_MIN_BUDGET_RATIO", 0.85)

    # Check for severely underfilled individual sections against outline budgets
    underfilled_sections = []
    if outline and outline.get("sections"):
        outline_budgets = {s.get("section_index"): s for s in outline["sections"]}
        for sec in completed_sections:
            s_idx = sec.get("section_index")
            s_def = outline_budgets.get(s_idx)
            if s_def:
                target_budget = s_def.get("word_budget", 0)
                sec_words = sec.get("word_count", 0)
                if target_budget >= 200 and (sec_words / float(target_budget)) < section_min_ratio:
                    underfilled_sections.append({
                        "section_index": s_idx,
                        "heading": sec.get("heading") or s_def.get("heading", f"Section {s_idx}"),
                        "purpose": sec.get("purpose") or s_def.get("purpose", ""),
                        "key_points": sec.get("key_points") or s_def.get("key_points", []),
                        "actual_words": sec_words,
                        "target_budget": target_budget,
                        "deficit": target_budget - sec_words,
                    })

    is_overall_underfilled = fill_ratio < underfill_tolerance
    if not is_overall_underfilled and not underfilled_sections:
        return None

    deficit = planned_target - total_words
    return {
        "total_words": total_words,
        "planned_target": planned_target,
        "fill_ratio": fill_ratio,
        "deficit": deficit,
        "underfilled_sections": underfilled_sections,
        "is_overall_underfilled": is_overall_underfilled,
    }


def _compute_evidence_relevance(
    sec_heading: str,
    sec_purpose: str,
    sec_key_points: list[str] | str | None,
    item: dict[str, Any],
    exclude_words: set[str] | None = None,
) -> float:
    """
    Score unused evidence against section heading, purpose, key points vs item title, focus area, and snippet.
    Returns deterministic token-overlap relevance score. Zero indicates no meaningful relationship.
    """
    kp_text = " ".join(sec_key_points) if isinstance(sec_key_points, list) else (sec_key_points or "")
    sec_text = f"{sec_heading} {sec_purpose} {kp_text}".lower()
    sec_words = {re.sub(r"[^\w\-]", "", w) for w in sec_text.split() if len(w) >= 3 and w not in COMMON_PHRASE_STOPWORDS}
    if exclude_words:
        sec_words = sec_words - exclude_words
    if not sec_words:
        return 0.0

    title_text = str(item.get("title", "")).lower()
    focus_text = str(item.get("focus_area", "")).lower()
    snippet_text = str(item.get("snippet", "")).lower()

    title_words = {re.sub(r"[^\w\-]", "", w) for w in title_text.split() if len(w) >= 3 and w not in COMMON_PHRASE_STOPWORDS}
    focus_words = {re.sub(r"[^\w\-]", "", w) for w in focus_text.split() if len(w) >= 3 and w not in COMMON_PHRASE_STOPWORDS}
    snippet_words = {re.sub(r"[^\w\-]", "", w) for w in snippet_text.split() if len(w) >= 3 and w not in COMMON_PHRASE_STOPWORDS}

    all_item_words = title_words | focus_words | snippet_words
    if exclude_words:
        all_item_words = all_item_words - exclude_words
    if not all_item_words:
        return 0.0

    shared = sec_words & all_item_words
    if not shared:
        return 0.0

    # Boost matches in title or focus_area
    title_focus_shared = sec_words & (title_words | focus_words)
    return float(len(shared) + 1.5 * len(title_focus_shared))


def expand_script_content_gap(
    job: PodcastJob,
    completed_sections: list[dict[str, Any]],
    gap_info: dict[str, Any],
    topic: str,
    evidence_packet: dict[str, Any],
    scope: EvidenceScope,
    db: Any = None,
    status_notifier: Any = None,
    return_metadata: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Fill genuine content gaps with targeted research or uncovered evidence rather than padding,
    strictly preserving existing section content.
    """
    sections = [dict(s) for s in completed_sections]
    deficit = max(250, min(1200, gap_info.get("deficit", 300)))
    total_words = gap_info.get("total_words", sum(s.get("word_count", 0) for s in sections))
    planned_target = gap_info.get("planned_target", total_words + deficit)
    fill_ratio = gap_info.get("fill_ratio", total_words / float(planned_target))

    covered_evidence_ids = {ev for s in sections for ev in s.get("relevant_evidence_ids", [])}
    all_packet_items = evidence_packet.get("items", []) if evidence_packet else []
    uncovered_items = [
        it for it in all_packet_items
        if it.get("evidence_id") and it["evidence_id"] not in covered_evidence_ids and not it.get("is_seed_source")
    ]

    underfilled_secs = gap_info.get("underfilled_sections", [])

    # Find relevant unused evidence for each underfilled section before research
    relevant_unused_before_by_sec: dict[int, list[dict[str, Any]]] = {}
    for s_info in underfilled_secs:
        s_idx = s_info["section_index"]
        target_s = next((s for s in sections if s.get("section_index") == s_idx), s_info)
        sh = target_s.get("heading", "")
        sp = target_s.get("purpose", "")
        skp = target_s.get("key_points", [])
        rel = [it for it in uncovered_items if _compute_evidence_relevance(sh, sp, skp, it) >= 1.0]
        relevant_unused_before_by_sec[s_idx] = rel

    # Sections lacking any relevant unused evidence
    sections_lacking_relevant_evidence = [
        s_info for s_info in underfilled_secs
        if not relevant_unused_before_by_sec.get(s_info["section_index"])
    ]

    relevant_unused_eids_before = sorted({
        it["evidence_id"]
        for rel_list in relevant_unused_before_by_sec.values()
        for it in rel_list
        if it.get("evidence_id")
    })

    # Step 1: Supplemental Research Decision
    # Supplemental research is triggered when:
    # - non-SOURCE_ONLY mode
    # - AND either:
    #   a) At least one underfilled section lacks relevant unused evidence (even if unrelated items exist globally!)
    #   b) The episode is overall underfilled and fewer than 2 uncovered items exist globally.
    supplemental_research_triggered = False
    supp_attempted = False
    supp_succeeded = False
    supp_failure_category = None
    new_supp_items: list[dict[str, Any]] = []
    supp_provider = None
    supp_model = None
    search_count = 0
    source_count = 0
    gap_focus = None
    valid_depth = None

    target_gap_secs = sections_lacking_relevant_evidence if sections_lacking_relevant_evidence else underfilled_secs
    affected_sections = [s.get("section_index") for s in target_gap_secs if s.get("section_index")]

    should_trigger_supp = (
        scope != EvidenceScope.SOURCE_ONLY
        and (
            bool(sections_lacking_relevant_evidence)
            or (gap_info.get("is_overall_underfilled") and len(uncovered_items) < 2)
        )
    )

    if should_trigger_supp:
        supplemental_research_triggered = True
        supp_attempted = True
        if status_notifier:
            status_notifier("Performing targeted research to address content gap...")

        gap_headings = []
        for s in target_gap_secs:
            sec_dict = next((sec for sec in sections if sec.get("section_index") == s.get("section_index")), None)
            h = (sec_dict.get("heading") if sec_dict else None) or s.get("heading") or f"Section {s.get('section_index', '')}"
            if h and h not in gap_headings:
                gap_headings.append(h)
        gap_focus = ", ".join(gap_headings) if gap_headings else topic
        logger.info(f"Targeted gap research triggered for job {job.id} on '{gap_focus}' (deficit={deficit} words).")

        covered_topics_summary = "\n".join(
            f"- Section {s.get('section_index', idx)}: {s.get('heading', '')} (Key focus: {s.get('purpose', 'N/A')})"
            for idx, s in enumerate(sections, 1)
        )

        supp_prompt = (
            f"Topic: {topic}\n"
            f"Target Gap Focus Area: {gap_focus}\n"
            f"Target Deficit: ~{deficit} words\n\n"
            f"ALREADY COVERED IN EPISODE (DO NOT REPEAT):\n{covered_topics_summary}\n\n"
            f"SUPPLEMENTAL RESEARCH CONTRACT:\n"
            f"1. Conduct targeted searches specifically for new, concrete, verifiable facts, data, and technical findings on '{gap_focus}'.\n"
            f"2. Do NOT recap or duplicate the topics already covered above.\n"
            f"3. Return specific grounded evidence items with source attribution."
        )

        configured_depth = getattr(settings, "HERALD_SUPPLEMENTAL_RESEARCH_DEPTH", "low")
        valid_depth = configured_depth.lower() if configured_depth and configured_depth.lower() in ("low", "medium", "high") else "low"

        def _do_supplemental_research(p_inst: Any, att: int, src: str) -> dict[str, Any]:
            nonlocal supp_provider, supp_model
            supp_provider = (
                getattr(p_inst, "provider_id", None)
                or getattr(p_inst, "provider_name", None)
                or getattr(p_inst, "name", None)
                or "gemini"
            ).lower()
            supp_model = (
                getattr(p_inst, "research_model", None)
                or getattr(p_inst, "configured_model", None)
                or getattr(p_inst, "model_name", None)
                or getattr(p_inst, "model", None)
            )
            return p_inst.generate_grounded_research(
                source_text=supp_prompt,
                research_depth=valid_depth,
                job_id=job.id,
                operation="targeted_gap_research",
                attempt=att,
            )

        try:
            supp_data = execute_with_failover(
                job=job,
                operation="targeted_gap_research",
                execute_fn=_do_supplemental_research,
                db=db,
                source_text=gap_focus,
                required_capability="research_grounding",
            )
            if supp_data and isinstance(supp_data, dict):
                g_meta = supp_data.get("grounding_metadata") or {}
                queries = g_meta.get("webSearchQueries") or g_meta.get("web_search_queries") or supp_data.get("queries") or []
                search_count = len(queries)
                supp_sources = supp_data.get("research_sources") or supp_data.get("sources") or []
                source_count = len(supp_sources)

                norm_supp = normalize_evidence_packet(
                    topic=gap_focus,
                    scope=scope,
                    grounded_research_data=supp_data,
                )
                supp_items = norm_supp.get("items") or supp_data.get("items") or []
                raw_supp_text = supp_data.get("raw_text") or ""

                existing_eids = {it.get("evidence_id") for it in all_packet_items}
                for idx, item in enumerate(supp_items, 1):
                    item_dict = dict(item)
                    new_eid = f"ev_supp_{idx}"
                    while new_eid in existing_eids:
                        idx += 1
                        new_eid = f"ev_supp_{idx}"
                    item_dict["evidence_id"] = new_eid
                    if not item_dict.get("focus_area") or item_dict["focus_area"] in ("Source Registry", "External Grounded Research"):
                        item_dict["focus_area"] = gap_focus
                    item_dict["is_seed_source"] = False
                    new_supp_items.append(item_dict)
                    existing_eids.add(new_eid)

                if not new_supp_items and raw_supp_text:
                    new_eid = f"ev_supp_{len(all_packet_items) + 1}"
                    first_supp_src = supp_sources[0] if supp_sources else {}
                    first_title = first_supp_src.get("title") if isinstance(first_supp_src, dict) else None
                    first_url = first_supp_src.get("url") if isinstance(first_supp_src, dict) else None
                    first_pub = first_supp_src.get("publisher") or first_supp_src.get("domain") if isinstance(first_supp_src, dict) else None
                    new_supp_items.append({
                        "evidence_id": new_eid,
                        "title": first_title or f"Targeted Findings on {gap_focus}",
                        "actual_source_title": first_title,
                        "publisher": first_pub or "Supplemental Grounded Research",
                        "source_url": first_url,
                        "snippet": raw_supp_text.strip(),
                        "is_seed_source": False,
                        "focus_area": gap_focus,
                    })

                if new_supp_items:
                    supp_succeeded = True
                    all_packet_items.extend(new_supp_items)
                    evidence_packet["items"] = all_packet_items
                    job.evidence_packet_json = evidence_packet
                    uncovered_items.extend(new_supp_items)
                    record_job_diagnostic_event(
                        job.id,
                        "INFO",
                        "research",
                        "SUPPLEMENTAL_RESEARCH_COMPLETED",
                        f"Targeted research pass acquired {len(new_supp_items)} new evidence items for '{gap_focus}'.",
                        metadata={
                            "new_evidence_count": len(new_supp_items),
                            "gap_focus": gap_focus,
                            "deficit": deficit,
                            "research_depth": valid_depth,
                        },
                        db=db,
                    )
                    if db:
                        db.commit()
        except Exception as supp_err:
            supp_failure_category = type(supp_err).__name__
            logger.warning(f"Targeted gap research failed non-fatally; proceeding with available evidence: {supp_err}")

    # Step 2: In-place section expansion with strictly relevant evidence (score >= 1.0)
    sections_expanded = []
    section_evidence_used = {}
    section_word_counts = []

    if underfilled_secs and uncovered_items:
        for sec_to_expand in underfilled_secs:
            s_idx = sec_to_expand["section_index"]
            target_sec_dict = next((s for s in sections if s.get("section_index") == s_idx), None)
            if not target_sec_dict:
                continue

            sec_h = target_sec_dict.get("heading", "")
            sec_p = target_sec_dict.get("purpose", "")
            sec_kp = target_sec_dict.get("key_points", [])

            # Filter uncovered items to ONLY relevant items (score >= 1.0)
            relevant_items = [
                it for it in uncovered_items
                if _compute_evidence_relevance(sec_h, sec_p, sec_kp, it) >= 1.0
            ]
            if not relevant_items:
                # NEVER use zero-relevance evidence merely because it is available! Leave section shorter.
                continue

            # Rank by relevance score descending
            relevant_items.sort(
                key=lambda it: _compute_evidence_relevance(sec_h, sec_p, sec_kp, it),
                reverse=True,
            )
            best_item = relevant_items[0]

            curr_narr = target_sec_dict.get("narration", "")
            curr_words = len(curr_narr.split())
            needed_words = sec_to_expand["deficit"]
            new_ev_snippet = best_item.get("snippet", "")
            ev_id_used = best_item.get("evidence_id")

            exp_prompt = f"""You are expanding Section {s_idx} ('{sec_h}') of a podcast about: {topic}
Section Purpose: {sec_p}

NEW EVIDENCE TO INTEGRATE:
\"{new_ev_snippet}\"

EXISTING DRAFT NARRATION (PRESERVE THIS CONTENT COMPLETELY):
\"\"\"
{curr_narr}
\"\"\"

EXPANSION CONTRACT:
1. Preserve all substantive facts, phrasing, and explanations already present in the draft narration.
2. Seamlessly integrate the new evidence to deepen and expand the analysis by approximately {needed_words} words.
3. Do NOT repeat or recap earlier sections. Maintain a natural, engaging spoken podcast rhythm.
4. Output the complete, expanded narration for Section {s_idx} only. Response-local numbering must begin with order=1.
"""
            def _exec_sec_expansion(p_inst: Any, attempt: int, src: str) -> PodcastScriptResponse:
                try:
                    resp = p_inst.generate_script(
                        source_text=src,
                        request_mode="standard",
                        source_title=topic,
                        job_id=job.id,
                        generation_instructions=exp_prompt,
                        is_isolated_section=True,
                        operation="gap_expansion",
                        attempt=attempt,
                    )
                except TypeError as te:
                    if "is_isolated_section" in str(te) or "operation" in str(te):
                        resp = p_inst.generate_script(
                            source_text=src,
                            request_mode="standard",
                            source_title=topic,
                            job_id=job.id,
                            generation_instructions=exp_prompt,
                        )
                    else:
                        raise
                if isinstance(resp, dict):
                    return parse_isolated_section_response(resp)
                return resp

            try:
                res: PodcastScriptResponse = execute_with_failover(
                    job=job,
                    operation="gap_expansion",
                    execute_fn=_exec_sec_expansion,
                    db=db,
                    source_text=new_ev_snippet,
                )
                exp_narr = "\n\n".join(seg.narration for seg in res.segments).strip()
                exp_words = len(exp_narr.split())
                if exp_words >= curr_words + 25:
                    target_sec_dict["narration"] = exp_narr
                    target_sec_dict["word_count"] = exp_words
                    if ev_id_used:
                        target_sec_dict.setdefault("relevant_evidence_ids", []).append(ev_id_used)
                        uncovered_items = [it for it in uncovered_items if it.get("evidence_id") != ev_id_used]
                    job.section_progress_json = sections
                    sections_expanded.append(s_idx)
                    section_evidence_used[str(s_idx)] = [ev_id_used]
                    section_word_counts.append({
                        "section_index": s_idx,
                        "before_words": curr_words,
                        "after_words": exp_words,
                    })
                    record_job_diagnostic_event(
                        job.id,
                        "INFO",
                        "duration",
                        "SECTION_EXPANDED_WITH_EVIDENCE",
                        f"Section {s_idx} expanded with grounded evidence ({curr_words} -> {exp_words} words).",
                        metadata={
                            "section_index": s_idx,
                            "words_added": exp_words - curr_words,
                            "evidence_id": ev_id_used,
                        },
                        db=db,
                    )
                    if db:
                        db.commit()
            except Exception as exp_err:
                logger.warning(f"In-place section expansion for section {s_idx} failed non-fatally: {exp_err}")

    # Step 3: Restricted Extra Section Fallback
    # A new section may be appended ONLY when:
    # 1. gap_info["is_overall_underfilled"] is true;
    # 2. Episode remains materially below target (< 80% tolerance) after in-place attempts;
    # 3. Proposed evidence is relevant to overall topic (score >= 1.0);
    # 4. Evidence represents a genuinely distinct narrative subject not belonging to existing sections (max existing sec score < 1.0).
    extra_section_added = False
    extra_section_reason = None
    curr_total_words = sum(s.get("word_count", 0) for s in sections)
    underfill_tolerance = getattr(settings, "HERALD_TOTAL_UNDERFILL_TOLERANCE_RATIO", 0.80)
    is_still_overall_underfilled = (curr_total_words / float(planned_target)) < underfill_tolerance

    if (
        gap_info.get("is_overall_underfilled")
        and is_still_overall_underfilled
        and uncovered_items
        and scope != EvidenceScope.SOURCE_ONLY
    ):
        # Look for genuinely distinct uncovered evidence relevant to topic but distinct from existing sections
        candidate_extra_item = None
        topic_words = {re.sub(r"[^\w\-]", "", w.lower()) for w in topic.split() if len(w) >= 3 and w.lower() not in COMMON_PHRASE_STOPWORDS}
        for it in uncovered_items:
            topic_rel = _compute_evidence_relevance(topic, "", None, it)
            if topic_rel >= 1.0:
                max_sec_rel = max(
                    (
                        _compute_evidence_relevance(
                            s.get("heading", ""),
                            s.get("purpose", ""),
                            s.get("key_points"),
                            it,
                            exclude_words=topic_words,
                        )
                        for s in sections
                    ),
                    default=0.0,
                )
                if max_sec_rel < 1.0:
                    candidate_extra_item = it
                    break

        if candidate_extra_item:
            uncovered_heading = candidate_extra_item.get("title") or f"Additional Findings on {topic}"
            logger.info(
                f"Long-form underfilled ({curr_total_words}/{planned_target} words) with distinct uncovered evidence. "
                f"Generating bounded extra section: {uncovered_heading}"
            )
            extra_deficit = planned_target - curr_total_words
            uncovered_sec_def = {
                "section_index": len(sections) + 1,
                "heading": uncovered_heading,
                "purpose": f"Analyze specific uncovered findings regarding: {candidate_extra_item.get('focus_area', topic)}.",
                "word_budget": extra_deficit,
                "word_budget_min": int(round(extra_deficit * 0.85)),
                "word_budget_max": int(round(extra_deficit * 1.15)),
                "relevant_evidence_ids": [candidate_extra_item.get("evidence_id")],
                "key_points": [candidate_extra_item.get("snippet", "")[:120]],
                "anti_repetition": "Focus strictly on newly introduced uncovered findings. Do not recap earlier sections.",
                "transition_intent": "Explore additional uncovered evidence",
            }
            covered_ctx = build_already_covered_context(
                sections,
                current_heading=uncovered_heading,
                current_purpose=uncovered_sec_def["purpose"],
                current_idx=uncovered_sec_def["section_index"],
            )
            try:
                extra_sec = generate_single_section(
                    job=job,
                    section_info=uncovered_sec_def,
                    topic=topic,
                    evidence_packet=evidence_packet,
                    previous_summary=covered_ctx,
                    scope=scope,
                    db=db,
                )
                if extra_sec.get("word_count", 0) > 100:
                    extra_sec["cumulative_words"] = curr_total_words + extra_sec.get("word_count", 0)
                    extra_sec["remaining_target"] = max(0, planned_target - extra_sec["cumulative_words"])
                    sections.append(extra_sec)
                    job.section_progress_json = sections
                    extra_section_added = True
                    extra_section_reason = "overall_underfill_with_distinct_topic_material"
                    record_job_diagnostic_event(
                        job.id,
                        "INFO",
                        "duration",
                        "UNCOVERED_EVIDENCE_SECTION_GENERATED",
                        f"Generated bounded extra section on uncovered evidence: {uncovered_heading}",
                        metadata={
                            "extra_section_heading": uncovered_heading,
                            "word_count": extra_sec.get("word_count", 0),
                            "uncovered_evidence_ids": uncovered_sec_def["relevant_evidence_ids"],
                        },
                        db=db,
                    )
                    if db:
                        db.commit()
            except Exception as extra_err:
                logger.warning(f"Uncovered material section generation failed non-fatally: {extra_err}")
        else:
            extra_section_reason = "no_distinct_uncovered_evidence"
    else:
        if not is_still_overall_underfilled:
            extra_section_reason = "acceptable_overall_duration"
        else:
            extra_section_reason = "no_uncovered_items_or_source_only"

    final_total_words = sum(s.get("word_count", 0) for s in sections)
    final_fill_ratio = final_total_words / float(planned_target)

    if not extra_section_added and (gap_info.get("is_overall_underfilled") or is_still_overall_underfilled):
        record_job_diagnostic_event(
            job.id,
            "INFO",
            "duration",
            "DURATION_UNDERFILL_ACCEPTED",
            f"Underfill accepted without extra section: {extra_section_reason} (words: {final_total_words}/{planned_target}).",
            metadata={
                "reason": extra_section_reason,
                "words": final_total_words,
                "target_words": planned_target,
            },
            db=db,
        )
        if db:
            try:
                db.commit()
            except Exception:
                pass

    # Machine-readable diagnostics dictionary
    gap_diag_meta = {
        "gap_detected": bool(gap_info.get("deficit", 0) > 0 or gap_info.get("is_overall_underfilled") or underfilled_secs),
        "triggered": supplemental_research_triggered,
        "attempted": supp_attempted,
        "succeeded": supp_succeeded,
        "provider": supp_provider,
        "model": supp_model,
        "search_count": search_count,
        "new_evidence_count": len(new_supp_items),
        "affected_sections": affected_sections,
        "failure_category": supp_failure_category,
        "initial_total_word_count": gap_info.get("total_words", total_words),
        "target_words": planned_target,
        "deficit": gap_info.get("deficit", deficit),
        "initial_fill_ratio": round(gap_info.get("fill_ratio", fill_ratio), 3),
        "fill_ratio": round(gap_info.get("fill_ratio", fill_ratio), 3),
        "underfilled_sections": [
            {
                "section_index": s["section_index"],
                "target_words": s.get("target_budget", 0),
                "original_words": s.get("actual_words", 0),
                "deficit": s.get("deficit", 0),
            }
            for s in underfilled_secs
        ],
        "relevant_unused_evidence_ids": relevant_unused_eids_before,
        "supplemental_research_triggered": supplemental_research_triggered,
        "research_depth": valid_depth,
        "source_count": source_count,
        "gap_focus": gap_focus,
        "supplemental_research": {
            "triggered": supplemental_research_triggered,
            "attempted": supp_attempted,
            "succeeded": supp_succeeded,
            "provider": supp_provider,
            "model": supp_model,
            "search_count": search_count,
            "source_count": source_count,
            "new_evidence_count": len(new_supp_items),
            "affected_sections": affected_sections,
            "failure_category": supp_failure_category,
            "gap_focus": gap_focus,
            "research_depth": valid_depth,
        },
        "normalized_supplemental_evidence_ids": [it["evidence_id"] for it in new_supp_items if it.get("evidence_id")],
        "supplemental_sources": [
            {
                "evidence_id": it.get("evidence_id"),
                "title": it.get("title") or it.get("actual_source_title"),
                "source_url": it.get("source_url"),
            }
            for it in new_supp_items
        ],
        "sections_expanded": sections_expanded,
        "section_evidence_used": section_evidence_used,
        "section_word_counts": section_word_counts,
        "extra_section_added": extra_section_added,
        "extra_section_reason": extra_section_reason,
        "extra_section_skipped_reason": (extra_section_reason if not extra_section_added else None),
        "final_total_words": final_total_words,
        "final_fill_ratio": round(final_fill_ratio, 3),
    }

    # Store gap diagnostics into configuration_state_json
    cfg = dict(job.configuration_state_json or {}) if isinstance(getattr(job, "configuration_state_json", None), dict) else {}
    cfg["gap_diagnostics"] = gap_diag_meta
    job.configuration_state_json = cfg
    if db:
        try:
            db.commit()
        except Exception:
            pass

    if return_metadata:
        return sections, gap_diag_meta
    return sections


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

    is_literal = (
        getattr(job, "content_mode", None) == ContentMode.LITERAL.value
        or str(getattr(job, "content_mode", "")).lower() == "literal"
        or str(getattr(job, "request_mode", "")).lower() == "literal"
    )

    effective_scope = scope
    if getattr(job, "research_degraded", False):
        effective_scope = EvidenceScope.SOURCE_ONLY

    # 2. Research Plan & Evidence Gathering
    _set_script_substage(job, "broad_research", db)
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
    _set_script_substage(job, "narrative_plan", db)
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

    # 4. Sequential Section Generation with Dynamic Budgeting and Checkpointing
    _set_script_substage(job, "section_generation", db)
    sections_def = outline.get("sections", [])
    completed_sections: list[dict[str, Any]] = list(job.section_progress_json or [])
    completed_indices = {s["section_index"] for s in completed_sections}

    requested_budget = get_target_word_budget(target_minutes)
    planned_target = outline.get("target_total_words") or requested_budget

    cumulative_words = sum(s.get("word_count", 0) for s in completed_sections)
    unwritten_sections_count = len([s for s in sections_def if s["section_index"] not in completed_indices])

    for sec_def in sections_def:
        sec_idx = sec_def["section_index"]
        if sec_idx in completed_indices:
            continue

        # Track active section in configuration state for truthful /status reporting
        cfg_curr = job.configuration_state_json or {}
        if not isinstance(cfg_curr, dict):
            cfg_curr = {}
        cfg_curr["script_current_section"] = sec_idx
        job.configuration_state_json = cfg_curr
        db.commit()

        # Dynamic remaining-budget redistribution:
        # Subsequent section targets adapt to actual output within sensible bounds
        if planned_target and unwritten_sections_count > 0:
            remaining_target = max(0, planned_target - cumulative_words)
            nominal_target = remaining_target // unwritten_sections_count

            # Determine baseline and bounding clamps
            base_sec_target = planned_target // len(sections_def)
            min_bound = max(180, int(base_sec_target * 0.45))
            max_bound = min(3000, max(min_bound + 100, int(base_sec_target * 1.6)))
            curr_target = max(min_bound, min(max_bound, nominal_target))

            sec_def["word_budget"] = curr_target
            sec_def["word_budget_min"] = int(round(curr_target * 0.85))
            sec_def["word_budget_max"] = int(round(curr_target * 1.15))

        if status_notifier:
            status_notifier(f"Writing section {sec_idx} of {len(sections_def)}: {sec_def['heading']}...")

        covered_ctx = build_already_covered_context(
            completed_sections,
            current_heading=sec_def.get("heading"),
            current_purpose=sec_def.get("purpose"),
            current_idx=sec_idx,
        )

        sec_result = generate_single_section(
            job=job,
            section_info=sec_def,
            topic=topic,
            evidence_packet=evidence_packet,
            previous_summary=covered_ctx if completed_sections else None,
            scope=effective_scope,
            db=db,
        )
        sec_words = sec_result.get("word_count", 0)

        # Controlled Section Expansion:
        # Check if actual generated words fall below approximately 85% of effective budget
        expansion_enabled = getattr(settings, "HERALD_SECTION_EXPANSION_ENABLED", True)
        min_ratio = getattr(settings, "HERALD_SECTION_MIN_BUDGET_RATIO", 0.85)
        allocated_target = sec_def.get("word_budget")
        threshold_words = int(round(allocated_target * min_ratio)) if allocated_target else None

        is_literal = (
            getattr(job, "content_mode", None) == ContentMode.LITERAL.value
            or str(getattr(job, "content_mode", "")).lower() == "literal"
        )

        if (
            expansion_enabled
            and not is_literal
            and allocated_target is not None
            and allocated_target >= 250
            and sec_words < threshold_words
        ):
            assigned_eids = sec_def.get("relevant_evidence_ids") or []
            assigned_kps = sec_def.get("key_points") or []
            if assigned_eids or assigned_kps:
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "duration",
                    "SECTION_EXPANSION_ATTEMPTED",
                    f"Section {sec_idx} generated {sec_words} words (<{min_ratio:.0%} of {allocated_target} target). Attempting controlled expansion.",
                    metadata={
                        "section_index": sec_idx,
                        "target_words": allocated_target,
                        "original_words": sec_words,
                        "threshold_words": threshold_words,
                    },
                    db=db,
                )
                exp_res = expand_single_section(
                    job=job,
                    section_info=sec_def,
                    current_narration=sec_result["narration"],
                    actual_words=sec_words,
                    target_budget=allocated_target,
                    topic=topic,
                    evidence_packet=evidence_packet,
                    scope=effective_scope,
                    covered_context=covered_ctx,
                    db=db,
                )
                if exp_res.get("success"):
                    sec_result["narration"] = exp_res["narration"]
                    sec_result["word_count"] = exp_res["word_count"]
                    sec_result["expansion_attempted"] = True
                    sec_result["expansion_words_added"] = exp_res.get("words_added", 0)
                    sec_result["expansion_final_words"] = exp_res["word_count"]
                    sec_words = exp_res["word_count"]
                    record_job_diagnostic_event(
                        job.id,
                        "INFO",
                        "duration",
                        "SECTION_EXPANSION_SUCCEEDED",
                        f"Section {sec_idx} expanded from {sec_words - exp_res.get('words_added', 0)} to {sec_words} words (+{exp_res.get('words_added', 0)} words).",
                        metadata={
                            "section_index": sec_idx,
                            "original_words": sec_words - exp_res.get("words_added", 0),
                            "final_words": sec_words,
                            "words_added": exp_res.get("words_added", 0),
                        },
                        db=db,
                    )
                else:
                    sec_result["expansion_attempted"] = True
                    sec_result["expansion_words_added"] = 0
                    sec_result["expansion_skipped_reason"] = exp_res.get("reason", "expansion_failed")
            else:
                sec_result["expansion_attempted"] = False
                sec_result["expansion_skipped_reason"] = "grounded_evidence_exhausted"
        else:
            sec_result["expansion_attempted"] = False
            sec_result["expansion_skipped_reason"] = (
                "budget_satisfied"
                if (threshold_words and sec_words >= threshold_words)
                else "expansion_disabled_or_not_applicable"
            )

        cumulative_words += sec_words
        unwritten_sections_count -= 1

        sec_result["cumulative_words"] = cumulative_words
        sec_result["remaining_target"] = max(0, planned_target - cumulative_words) if planned_target else None
        completed_sections.append(sec_result)
        job.section_progress_json = completed_sections
        db.commit()

    # Clear active section tracking now that section generation loop is complete
    cfg_curr = job.configuration_state_json or {}
    if isinstance(cfg_curr, dict) and "script_current_section" in cfg_curr:
        cfg_curr.pop("script_current_section", None)
        job.configuration_state_json = cfg_curr
        db.commit()

    # 5. Initial Script Assembly & Smoothing
    _set_script_substage(job, "script_assembly", db)
    if status_notifier:
        status_notifier("Assembling initial podcast script...")

    initial_title = clean_metadata_scaffolding(job.custom_title or topic)
    assembled_script = assemble_and_smooth_script(
        episode_title=initial_title,
        episode_description=outline.get("episode_description", f"Episode about {topic}"),
        sections=completed_sections,
        source_title=source_title,
        planned_target_words=planned_target,
    )
    job.script_json = assembled_script.model_dump()
    db.commit()

    # 6. Quality Gate: Anti-Repetition & Structural Checks
    _set_script_substage(job, "quality_gate", db)
    from herald.services.quality_gate import run_quality_gate

    cleaned_script, q_report = run_quality_gate(
        job.script_json,
        job=job,
        outline=outline,
    )
    job.script_json = cleaned_script
    job._recent_distinctive_warnings = q_report.distinctive_phrase_warnings
    metadata_cleanup_needed = q_report.metadata_cleanup_recommended

    # 7. Post-Generation Repetition Cleanup (rewriting only later section, bounded 1 pass)
    _set_script_substage(job, "repetition_repair", db)
    duplicate_repair_enabled = getattr(settings, "HERALD_DUPLICATE_REPAIR_ENABLED", True)
    repaired_sections = list(completed_sections)
    rep_diag: dict[str, Any] = {
        "candidate_warnings_count": len(q_report.near_duplicate_warnings) + len(q_report.distinctive_phrase_warnings),
        "review_performed": False,
        "substantive_duplicates_found": 0,
        "repaired_count": 0,
    }
    if duplicate_repair_enabled and not is_literal:
        dup_warns = q_report.near_duplicate_warnings
        distinctive_warns = q_report.distinctive_phrase_warnings

        # Run structured repetition review to filter candidates
        review_res = None
        confirmed_candidates = []
        if dup_warns or distinctive_warns:
            review_res, confirmed_candidates, rep_meta = review_script_repetition(
                job=job,
                completed_sections=repaired_sections,
                near_duplicate_warnings=dup_warns,
                distinctive_phrase_warnings=distinctive_warns,
                topic=topic,
                db=db,
            )
            rep_diag.update({
                "initial_near_duplicate_warnings_count": rep_meta.get("initial_near_duplicate_warnings_count", len(dup_warns)),
                "distinctive_concept_candidates_count": rep_meta.get("distinctive_concept_candidates_count", len(distinctive_warns)),
                "distinctive_concept_candidates": rep_meta.get("distinctive_concept_candidates", []),
                "candidate_ranking_selection": rep_meta.get("candidate_ranking_selection", []),
                "candidate_count_evaluated": rep_meta.get("evaluated_count", 0),
                "omitted_candidates": rep_meta.get("omitted_candidates", []),
                "review_results": [r.model_dump() for r in review_res.reviews] if review_res else [],
                "confirmed_substantive_repetitions": [
                    {
                        "section_a": c["metadata"]["section_a"],
                        "section_b": c["metadata"]["section_b"],
                        "concept": c["metadata"].get("concept") or c["metadata"].get("passage_b"),
                        "explanation": c["metadata"].get("explanation"),
                    }
                    for c in confirmed_candidates
                ],
                "review_performed": (review_res is not None),
                "substantive_duplicates_found": len(confirmed_candidates),
                "total_candidate_count": rep_meta.get("total_candidate_count", 0),
            })

        if confirmed_candidates:
            repaired_secs, dup_meta = repair_script_duplicates(
                job=job,
                sections=repaired_sections,
                duplicate_warnings=confirmed_candidates,
                evidence_packet=evidence_packet,
                topic=topic,
                scope=effective_scope,
                db=db,
            )
            rep_diag.update({
                "repaired_count": dup_meta.get("repaired_count", 0),
                "repaired_section_indexes": dup_meta.get("repaired_section_indices", []),
                "repaired_section_word_counts": dup_meta.get("repaired_section_word_counts", []),
                "repair_success": dup_meta.get("repair_success", False),
            })
            if dup_meta.get("repaired_count", 0) > 0:
                repaired_sections = repaired_secs
                assembled_script = assemble_and_smooth_script(
                    episode_title=job.script_json.get("episode_title", topic),
                    episode_description=outline.get("episode_description", f"Episode about {topic}"),
                    sections=repaired_sections,
                    source_title=source_title,
                    planned_target_words=planned_target,
                )
                job.script_json = assembled_script.model_dump()
                cleaned_script, q_report = run_quality_gate(
                    job.script_json,
                    job=job,
                    outline=outline,
                )
                job.script_json = cleaned_script
                rep_diag["final_repetition_findings"] = {
                    "remaining_near_duplicate_warnings": len(q_report.near_duplicate_warnings),
                    "remaining_distinctive_phrase_warnings": len(q_report.distinctive_phrase_warnings),
                }
                record_job_diagnostic_event(
                    job.id,
                    "INFO",
                    "quality_gate",
                    "DUPLICATE_REPAIR_PERFORMED",
                    f"Pre-TTS duplicate repair completed: repaired {dup_meta.get('repaired_count')} sections; remaining warnings: {len(q_report.near_duplicate_warnings)}",
                    metadata={
                        "original_warnings": dup_meta.get("original_warnings_count"),
                        "repaired_sections": dup_meta.get("repaired_count"),
                        "remaining_warnings": len(q_report.near_duplicate_warnings),
                    },
                    db=db,
                )

    # 8. Content Gap Research & Targeted Expansion
    # Evaluates total generated words vs planned budget using centralized tolerances.
    # Fills genuine gaps with fresh research/uncovered evidence rather than artificial padding,
    # strictly preserving existing section content.
    _set_script_substage(job, "gap_expansion", db)
    gap_info = detect_content_gap(repaired_sections, planned_target, outline)
    gap_diag: dict[str, Any] = {
        "gap_detected": gap_info is not None,
        "deficit": gap_info.get("deficit") if gap_info else 0,
        "fill_ratio": round(gap_info.get("fill_ratio", 1.0), 3) if gap_info else 1.0,
        "expansion_performed": False,
        "new_evidence_used": False,
    }
    if gap_info and not is_literal:
        before_words = sum(s.get("word_count", 0) for s in repaired_sections)
        before_eids = {ev for s in repaired_sections for ev in s.get("relevant_evidence_ids", [])}
        expanded_sections, gap_diag_meta = expand_script_content_gap(
            job=job,
            completed_sections=repaired_sections,
            gap_info=gap_info,
            topic=topic,
            evidence_packet=evidence_packet,
            scope=effective_scope,
            db=db,
            status_notifier=status_notifier,
            return_metadata=True,
        )
        gap_diag.update(gap_diag_meta)
        after_words = sum(s.get("word_count", 0) for s in expanded_sections)
        after_eids = {ev for s in expanded_sections for ev in s.get("relevant_evidence_ids", [])}
        if after_words > before_words or len(expanded_sections) > len(repaired_sections):
            gap_diag["expansion_performed"] = True
            gap_diag["words_added"] = after_words - before_words
            gap_diag["new_evidence_used"] = bool(after_eids - before_eids)
            repaired_sections = expanded_sections
            assembled_script = assemble_and_smooth_script(
                episode_title=job.script_json.get("episode_title", topic),
                episode_description=outline.get("episode_description", f"Episode about {topic}"),
                sections=repaired_sections,
                source_title=source_title,
                planned_target_words=planned_target,
            )
            job.script_json = assembled_script.model_dump()
    elif not gap_info and planned_target and planned_target > 500:
        tot_w = sum(s.get("word_count", 0) for s in repaired_sections)
        logger.info(
            f"Long-form generation reached {tot_w / float(planned_target):.1%} of target "
            f"({tot_w}/{planned_target} words). Accepted cleanly."
        )

    # 9. Semantic Fidelity Audit & Bounded Repair
    # Placed AFTER repetition repair and gap expansion so all final content is audited.
    _set_script_substage(job, "fidelity_audit", db)
    if status_notifier:
        status_notifier("Verifying source coverage and factual fidelity...")

    repaired_sections, audit_res = audit_and_repair_fidelity(
        job=job,
        sections=repaired_sections,
        source_ledger=source_ledger,
        evidence_packet=evidence_packet,
        scope=effective_scope,
        db=db,
        source_text=source_text,
    )
    job.fidelity_audit_json = audit_res

    # If fidelity repair updated sections, reassemble script and rerun local quality gate
    if audit_res.get("repair_succeeded"):
        assembled_script = assemble_and_smooth_script(
            episode_title=job.script_json.get("episode_title", topic),
            episode_description=outline.get("episode_description", f"Episode about {topic}"),
            sections=repaired_sections,
            source_title=source_title,
            planned_target_words=planned_target,
        )
        job.script_json = assembled_script.model_dump()
        cleaned_script, q_report = run_quality_gate(
            job.script_json,
            job=job,
            outline=outline,
            fidelity_audit=audit_res,
        )
        job.script_json = cleaned_script

    # 10. Final Quality Check & Title/Heading Metadata Cleanup
    # Executes after fidelity repair/reassembly.
    # Runs for all non-literal jobs where job.custom_title is None (producing natural 3-8 word titles),
    # or whenever quality gate recommends metadata cleanup.
    _set_script_substage(job, "final_quality_check", db)
    cleaned_script, q_report = run_quality_gate(
        job.script_json,
        job=job,
        outline=outline,
        fidelity_audit=audit_res,
    )
    job.script_json = cleaned_script

    has_explicit_custom_title = bool(getattr(job, "custom_title", None) and str(job.custom_title).strip())
    should_run_metadata_cleanup = (
        not is_literal
        and (
            not has_explicit_custom_title
            or q_report.metadata_cleanup_recommended
            or metadata_cleanup_needed
        )
    )

    if should_run_metadata_cleanup:
        cleanup_res = cleanup_script_metadata(
            job=job,
            script_dict=dict(job.script_json),
            topic=topic,
            scope=effective_scope,
            db=db,
            return_metadata=True,
        )
        if isinstance(cleanup_res, tuple) and len(cleanup_res) == 2:
            pol_script, clean_meta = cleanup_res
        else:
            pol_script = cleanup_res
            clean_meta = {"performed": True} if isinstance(cleanup_res, dict) else {}
        job.script_json = pol_script
        cleaned_script, q_report = run_quality_gate(
            job.script_json,
            job=job,
            outline=outline,
            fidelity_audit=audit_res,
        )
        job.script_json = cleaned_script
        if clean_meta.get("performed"):
            record_job_diagnostic_event(
                job.id,
                "INFO",
                "quality_gate",
                "METADATA_CLEANUP_PERFORMED",
                f"Polished episode metadata: title='{job.script_json.get('episode_title')}'",
                metadata=clean_meta,
                db=db,
            )
        elif clean_meta.get("failed"):
            record_job_diagnostic_event(
                job.id,
                "WARNING",
                "quality_gate",
                "METADATA_CLEANUP_FAILED",
                f"Metadata cleanup failed: {clean_meta.get('error') or 'non-fatal failure'}",
                metadata=clean_meta,
                db=db,
            )
        else:
            record_job_diagnostic_event(
                job.id,
                "INFO",
                "quality_gate",
                "METADATA_CLEANUP_SKIPPED",
                "Metadata cleanup skipped (no changes needed or capability unavailable).",
                metadata=clean_meta,
                db=db,
            )
    else:
        record_job_diagnostic_event(
            job.id,
            "INFO",
            "quality_gate",
            "METADATA_CLEANUP_SKIPPED",
            "Metadata cleanup skipped (literal mode or cleanup not requested).",
            metadata={"reason": "literal_mode" if is_literal else "not_requested"},
            db=db,
        )

    if q_report.has_warnings:
        record_job_diagnostic_event(
            job.id,
            "WARNING",
            "quality_gate",
            "QUALITY_GATE_WARNINGS",
            f"Quality gate identified {len(q_report.warnings)} issues: {', '.join(w.message for w in q_report.warnings[:3])}",
            metadata={"warning_count": len(q_report.warnings), "warnings": [w.to_dict() for w in q_report.warnings]},
            db=db,
        )

    # Duration calculation
    from herald.services.eta_calculator import calculate_script_duration

    dur_info = calculate_script_duration(
        job.script_json, job.custom_speed or settings.KOKORO_SPEED
    )
    job.program_duration_seconds = dur_info.get("predicted_duration_seconds")

    if audit_res.get("content_warning"):
        record_job_diagnostic_event(
            job.id,
            "WARNING",
            "fidelity",
            "CONTENT_WARNING_FLAGGED",
            f"Unresolved factual or fidelity concern remains after audit: {audit_res.get('repair_instructions', 'Fidelity issue')}",
            metadata={"audit_status": audit_res.get("status"), "repair_instructions": audit_res.get("repair_instructions")},
            db=db,
        )

    # Record duration & configuration telemetry
    cfg_state = job.configuration_state_json or {}
    if not isinstance(cfg_state, dict):
        cfg_state = {}
    cfg_state.update({
        "requested_target_words": requested_budget,
        "evidence_supported_target_words": planned_target,
        "actual_words": sum(s.get("word_count", 0) for s in repaired_sections),
        "duration_estimation": dur_info,
        "quality_gate": q_report.to_dict(),
        "content_warning": bool(audit_res.get("content_warning")),
        "script_substage": "complete",
        "script_current_section": None,
        "repetition_diagnostics": rep_diag,
        "gap_diagnostics": gap_diag,
    })
    job.configuration_state_json = cfg_state
    db.commit()

    if isinstance(job.script_json, dict) and "segments" in job.script_json and "episode_description" in job.script_json and "warnings" in job.script_json:
        try:
            final_script = PodcastScriptResponse(**job.script_json)
        except Exception:
            final_script = assemble_and_smooth_script(
                episode_title=job.script_json.get("episode_title", topic),
                episode_description=outline.get("episode_description", f"Episode about {topic}"),
                sections=repaired_sections,
                source_title=source_title,
                planned_target_words=planned_target,
            )
            job.script_json = final_script.model_dump()
    else:
        final_script = assemble_and_smooth_script(
            episode_title=job.script_json.get("episode_title", topic) if isinstance(job.script_json, dict) else topic,
            episode_description=outline.get("episode_description", f"Episode about {topic}"),
            sections=repaired_sections,
            source_title=source_title,
            planned_target_words=planned_target,
        )
        job.script_json = final_script.model_dump()
    return final_script

