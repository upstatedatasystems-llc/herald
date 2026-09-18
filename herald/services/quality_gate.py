"""Local Script Quality Gate for Herald.

Deterministic, zero-AI script quality inspection before user approval and TTS synthesis.
Detects:
- Duplicate headings
- Near-duplicate paragraphs / passages (Jaccard n-gram overlap)
- Repetitive opening phrases across sections
- Excessively long run-on sentences
- Section word budget divergence (<35% or >250% of nominal target)
- Generic catch-up headings
- Accidental raw metadata prefixes (with safe deterministic cleanup)
- Suspicious TTS-risk markup (unrendered tables, raw URLs, unparsed tags)
- Duration divergence against requested target
- Material unresolved fidelity findings
"""

import enum
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from herald.services.eta_calculator import calculate_script_duration

logger = logging.getLogger("herald.services.quality_gate")

GENERIC_CATCHUP_PATTERNS = [
    r"comprehensive analysis and evidence synthesis",
    r"evidence synthesis and analysis",
    r"deficit recovery",
    r"catch-?up analysis",
    r"supplemental analysis and recap",
]

RAW_PREFIX_PATTERNS = [
    r"^(?:topic|subject|title|heading|header|narration|script)\s*[:.\-–—]\s*",
    r"^(?:grounded\s+finding|key\s+finding|finding|key\s+takeaway|takeaway)\s*(?:\d+|[ivxlcdm]+)?\s*[:.\-–—]\s*",
    r"^(?:section|chapter|part|segment)\s+(?:\d+|[ivxlcdm]+)\s*[:.\-–—]\s*",
]


def clean_metadata_scaffolding(text: str) -> str:
    """
    Deterministically strip scaffolding prefixes (e.g. 'Grounded Finding 1:', 'Chapter 2:', 'Section 3:')
    and dangling trailing punctuation from titles and headings.
    """
    if not text:
        return ""
    cleaned = text.strip()
    for pat in RAW_PREFIX_PATTERNS:
        if re.search(pat, cleaned, re.IGNORECASE):
            cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()
            break
    cleaned = re.sub(r"[:,\-–—\.]\s*$", "", cleaned).strip()
    return cleaned

TTS_RISK_PATTERNS = [
    (r"https?://\S+", "Unexpanded raw URL detected in narration"),
    (r"\|(?:\s*[^|\r\n]+\s*\|){2,}", "Unrendered Markdown table detected in narration"),
    (r"<(?:source_data|trusted_generation_instructions|script|system_override)[^>]*>", "Unparsed XML/platform tag in narration"),
    (r"\[latex\].*?\[/latex\]", "Unparsed LaTeX markup in narration"),
]


class QualityStatus(str, enum.Enum):
    PASS = "PASS"
    WARN = "WARN"


class QualitySeverity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class QualityWarning(BaseModel):
    code: str
    message: str
    section_index: int | None = None
    severity: QualitySeverity = QualitySeverity.WARNING
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class QualityReport(BaseModel):
    status: QualityStatus
    warnings: list[QualityWarning] = Field(default_factory=list)
    cleanups_applied: list[str] = Field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    @property
    def distinctive_phrase_warnings(self) -> list[QualityWarning]:
        return [w for w in self.warnings if w.code == "REPEATED_DISTINCTIVE_PHRASE"]

    @property
    def near_duplicate_warnings(self) -> list[QualityWarning]:
        return [w for w in self.warnings if w.code == "NEAR_DUPLICATE_PASSAGE"]

    @property
    def duplicate_repair_recommended(self) -> bool:
        dup_warns = self.near_duplicate_warnings
        from herald.config import settings

        threshold = getattr(settings, "HERALD_DUPLICATE_REPAIR_COUNT_THRESHOLD", 3)
        if len(dup_warns) > threshold:
            return True
        for w in dup_warns:
            sim = w.metadata.get("similarity", 0.0)
            if sim >= 0.75:
                return True
        return False

    @property
    def metadata_cleanup_recommended(self) -> bool:
        meta_codes = {
            "SUSPICIOUS_TITLE_TRUNCATION",
            "SUSPICIOUS_HEADING_TRUNCATION",
            "GENERIC_PART_HEADING",
            "GENERIC_CATCHUP_HEADING",
            "DUPLICATE_HEADING",
            "EXCESSIVE_TITLE_LENGTH",
            "EXCESSIVE_HEADING_LENGTH",
        }
        return any(w.code in meta_codes for w in self.warnings)

    def to_dict(self) -> dict[str, Any]:
        d = self.model_dump()
        d["duplicate_repair_recommended"] = self.duplicate_repair_recommended
        d["metadata_cleanup_recommended"] = self.metadata_cleanup_recommended
        d["distinctive_phrase_warnings"] = [w.to_dict() for w in self.distinctive_phrase_warnings]
        return d


COMMON_PHRASE_STOPWORDS = {
    "a", "an", "the", "in", "on", "at", "by", "for", "with", "about", "against",
    "between", "into", "through", "during", "before", "after", "above", "below",
    "to", "from", "up", "down", "out", "off", "over", "under", "again", "further",
    "then", "once", "here", "there", "when", "where", "why", "how", "all", "any",
    "both", "each", "few", "more", "most", "other", "some", "such", "no", "nor",
    "not", "only", "own", "same", "so", "than", "too", "very", "s", "t", "can",
    "will", "just", "don", "should", "now", "and", "but", "if", "or", "because",
    "as", "until", "while", "of", "it", "its", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "having", "do", "does", "did", "doing",
    "would", "could", "ought", "i", "you", "he", "she", "we", "they", "this",
    "that", "these", "those",
}


def _extract_distinctive_phrases(text: str, min_words: int = 4, max_words: int = 6) -> set[str]:
    """Extract distinctive 4-6 word candidate phrases, filtering out common stop-word boilerplate."""
    raw_tokens = text.split()
    words = [re.sub(r"[^\w\-]", "", w.lower()) for w in raw_tokens]
    words = [w for w in words if w]
    if len(words) < min_words:
        return set()
    phrases = set()
    for n in range(min_words, max_words + 1):
        for i in range(len(words) - n + 1):
            ngram = words[i : i + n]
            non_stop = [w for w in ngram if w not in COMMON_PHRASE_STOPWORDS]
            if len(non_stop) >= 2 and len(non_stop) / len(ngram) >= 0.4:
                phrases.add(" ".join(ngram))
    return phrases


def _extract_trigrams(text: str) -> set[tuple[str, str, str]]:
    words = [w.lower().strip(".,;:!?\"'()") for w in text.split() if len(w) > 1]
    if len(words) < 3:
        return set()
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def run_quality_gate(
    script_dict: dict[str, Any],
    job: Any = None,
    outline: dict[str, Any] | None = None,
    fidelity_audit: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], QualityReport]:
    """
    Run deterministic local quality gate on script dictionary.
    Returns:
        (cleaned_script_dict, quality_report)
    Zero LLM calls are made.
    """
    warnings: list[QualityWarning] = []
    cleanups: list[str] = []

    if not script_dict or not isinstance(script_dict, dict):
        warnings.append(
            QualityWarning(
                code="EMPTY_SCRIPT",
                message="Script dictionary is empty or malformed.",
                severity=QualitySeverity.CRITICAL,
            )
        )
        return script_dict or {}, QualityReport(status=QualityStatus.WARN, warnings=warnings)

    cleaned_script = dict(script_dict)
    segments = list(cleaned_script.get("segments") or [])
    cleaned_segments = []

    # 1. Clean raw metadata prefix from episode_title
    ep_title = (cleaned_script.get("episode_title") or "").strip()
    for pat in RAW_PREFIX_PATTERNS:
        if re.search(pat, ep_title, re.IGNORECASE):
            old_title = ep_title
            ep_title = re.sub(pat, "", ep_title, flags=re.IGNORECASE).strip()
            cleanups.append(f"Stripped raw prefix from episode title ('{old_title}' -> '{ep_title}')")
            break

    # Clean dangling trailing punctuation from title
    ep_title_clean = re.sub(r"[:,\-–—\.]\s*$", "", ep_title).strip()
    if ep_title_clean != ep_title:
        cleanups.append(f"Stripped trailing punctuation from episode title ('{ep_title}' -> '{ep_title_clean}')")
        ep_title = ep_title_clean
    cleaned_script["episode_title"] = ep_title

    # Title truncation / quality checks
    if ep_title:
        if ep_title.endswith("-") or re.search(r"\b(?:the|a|an|of|in|and|or|for|to|with|by|from)\s*$", ep_title, re.IGNORECASE):
            warnings.append(
                QualityWarning(
                    code="SUSPICIOUS_TITLE_TRUNCATION",
                    message=f"Episode title appears truncated: '{ep_title}'",
                    severity=QualitySeverity.WARNING,
                )
            )
        if len(ep_title) > 130:
            warnings.append(
                QualityWarning(
                    code="EXCESSIVE_TITLE_LENGTH",
                    message=f"Episode title exceeds 130 characters ({len(ep_title)} chars): '{ep_title}'",
                    severity=QualitySeverity.WARNING,
                )
            )

    # 2. Check headings and narration across segments
    seen_headings: dict[str, int] = {}
    paragraphs_pool: list[tuple[int, str, set[tuple[str, str, str]]]] = []
    openings_pool: list[tuple[int, str]] = []
    section_phrases_pool: dict[int, set[str]] = {}

    for idx, seg in enumerate(segments, 1):
        if not isinstance(seg, dict):
            continue
        seg_clean = dict(seg)
        h_raw = (seg_clean.get("heading") or f"Section {idx}").strip()

        # Clean raw prefixes from heading
        for pat in RAW_PREFIX_PATTERNS:
            if re.search(pat, h_raw, re.IGNORECASE):
                old_h = h_raw
                h_raw = re.sub(pat, "", h_raw, flags=re.IGNORECASE).strip()
                cleanups.append(f"Stripped raw prefix from section {idx} heading ('{old_h}' -> '{h_raw}')")
                break

        # Clean dangling trailing punctuation from heading
        h_clean = re.sub(r"[:,\-–—\.]\s*$", "", h_raw).strip()
        if h_clean != h_raw:
            cleanups.append(f"Stripped trailing punctuation from section {idx} heading ('{h_raw}' -> '{h_clean}')")
            h_raw = h_clean
        seg_clean["heading"] = h_raw

        # Check for generic catch-up headings
        h_lower = h_raw.lower()
        for pat in GENERIC_CATCHUP_PATTERNS:
            if re.search(pat, h_lower):
                warnings.append(
                    QualityWarning(
                        code="GENERIC_CATCHUP_HEADING",
                        message=f"Section {idx} uses generic deficit catch-up heading: '{h_raw}'",
                        section_index=idx,
                        severity=QualitySeverity.WARNING,
                    )
                )

        # Check for generic continuation / Part N headings
        if re.match(r"^(?:part\s+\d+|reading\s+part\s+\d+|\(part\s+\d+\))$", h_lower) or re.search(r"\s*\(part\s+\d+\)$", h_lower):
            warnings.append(
                QualityWarning(
                    code="GENERIC_PART_HEADING",
                    message=f"Section {idx} uses generic continuation heading: '{h_raw}'",
                    section_index=idx,
                    severity=QualitySeverity.WARNING,
                )
            )

        # Check for heading truncation or excessive length
        if re.search(r"\b(?:the|a|an|of|in|and|or|for|to|with|by|from)\s*$", h_raw, re.IGNORECASE) or h_raw.endswith("-"):
            warnings.append(
                QualityWarning(
                    code="SUSPICIOUS_HEADING_TRUNCATION",
                    message=f"Section {idx} heading appears truncated: '{h_raw}'",
                    section_index=idx,
                    severity=QualitySeverity.WARNING,
                )
            )
        if len(h_raw) > 90:
            warnings.append(
                QualityWarning(
                    code="EXCESSIVE_HEADING_LENGTH",
                    message=f"Section {idx} heading exceeds 90 characters ({len(h_raw)} chars): '{h_raw}'",
                    section_index=idx,
                    severity=QualitySeverity.WARNING,
                )
            )

        # Duplicate heading check
        norm_h = re.sub(r"\s+", " ", h_lower).strip()
        if norm_h in seen_headings:
            prior_idx = seen_headings[norm_h]
            warnings.append(
                QualityWarning(
                    code="DUPLICATE_HEADING",
                    message=f"Section {idx} repeats heading from Section {prior_idx}: '{h_raw}'",
                    section_index=idx,
                    severity=QualitySeverity.WARNING,
                    metadata={"prior_section": prior_idx},
                )
            )
        else:
            seen_headings[norm_h] = idx

        # Process narration
        narr_raw = (seg_clean.get("narration") or "").strip()
        # Clean raw prefixes from narration start
        for pat in RAW_PREFIX_PATTERNS:
            if re.search(pat, narr_raw, re.IGNORECASE):
                old_narr_prefix = narr_raw[:30]
                narr_raw = re.sub(pat, "", narr_raw, flags=re.IGNORECASE).strip()
                cleanups.append(f"Stripped raw prefix from section {idx} narration start ('{old_narr_prefix}')")
                break
        seg_clean["narration"] = narr_raw

        # Check TTS-risk markup
        for pat, desc in TTS_RISK_PATTERNS:
            if re.search(pat, narr_raw, re.IGNORECASE):
                if "URL" in desc:
                    w_code = "TTS_UNEXPANDED_URL"
                elif "Markdown" in desc or "tag" in desc or "LaTeX" in desc:
                    w_code = "TTS_SUSPICIOUS_MARKUP"
                else:
                    w_code = "TTS_RISK_MARKUP"
                warnings.append(
                    QualityWarning(
                        code=w_code,
                        message=f"Section {idx} contains risky TTS markup: {desc}",
                        section_index=idx,
                        severity=QualitySeverity.WARNING,
                    )
                )

        # Sentence-level checks (run-on sentence without punctuation)
        narr_words = narr_raw.split()
        seg_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", narr_raw) if s.strip()]
        for sent in seg_sentences:
            s_words = sent.split()
            # If sentence is >= 42 words and has no comma, colon, semicolon, or dash
            if len(s_words) >= 42 and not any(ch in sent for ch in (",", ";", ":", "—", "-")):
                warnings.append(
                    QualityWarning(
                        code="RUN_ON_SENTENCE",
                        message=f"Section {idx} contains run-on sentence ({len(s_words)} words without internal punctuation).",
                        section_index=idx,
                        severity=QualitySeverity.WARNING,
                    )
                )

        # Repetitive opening check (first 4 words)
        if len(narr_words) >= 4:
            first_4 = " ".join(w.lower().strip(".,;:!?\"'") for w in narr_words[:4])
            openings_pool.append((idx, first_4))

        # Paragraphs / text segments for near-duplicate detection
        pars = [p.strip() for p in re.split(r"\n\s*\n", narr_raw) if len(p.split()) >= 12] or [narr_raw]
        for p in pars:
            trigrams = _extract_trigrams(p)
            if trigrams:
                paragraphs_pool.append((idx, p, trigrams))

        # Distinctive phrase extraction for lightweight advisory repetition detection
        sec_phrases = _extract_distinctive_phrases(narr_raw)
        if sec_phrases:
            section_phrases_pool[idx] = sec_phrases

        cleaned_segments.append(seg_clean)

    cleaned_script["segments"] = cleaned_segments

    # 3. Check for repetitive openings across sections
    opening_counts: dict[str, list[int]] = {}
    for s_idx, op in openings_pool:
        opening_counts.setdefault(op, []).append(s_idx)
    for op_phrase, s_indices in opening_counts.items():
        if len(s_indices) >= 3:
            warnings.append(
                QualityWarning(
                    code="REPETITIVE_OPENING",
                    message=f"Sections {s_indices} share repetitive opening phrase: '{op_phrase} ...'",
                    severity=QualitySeverity.WARNING,
                    metadata={"sections": s_indices, "phrase": op_phrase},
                )
            )

    # 4. Check for near-duplicate passages across distinct sections
    for i in range(len(paragraphs_pool)):
        idx_a, text_a, tri_a = paragraphs_pool[i]
        for j in range(i + 1, len(paragraphs_pool)):
            idx_b, text_b, tri_b = paragraphs_pool[j]
            if idx_a == idx_b:
                continue
            inter = len(tri_a.intersection(tri_b))
            union = len(tri_a.union(tri_b))
            if union > 0:
                jaccard = inter / float(union)
                if jaccard >= 0.60 or inter >= 10:
                    warnings.append(
                        QualityWarning(
                            code="NEAR_DUPLICATE_PASSAGE",
                            message=f"Sections {idx_a} and {idx_b} contain near-duplicate passages ({jaccard:.0%} similarity).",
                            severity=QualitySeverity.WARNING,
                            metadata={
                                "section_a": idx_a,
                                "section_b": idx_b,
                                "similarity": round(jaccard, 3),
                                "passage_a": text_a[:180],
                                "passage_b": text_b[:180],
                                "intersection_count": inter,
                            },
                        )
                    )

    # 5. Advisory Distinctive Phrase / Concept Detector
    # Evaluates distinctive 4-6 word n-grams repeated across multiple distinct sections.
    # Strictly advisory (severity=QualitySeverity.INFO) so legitimate terminology does not trigger unwanted repair.
    phrase_to_sections: dict[str, set[int]] = {}
    for s_idx, p_phrases in section_phrases_pool.items():
        for ph in p_phrases:
            phrase_to_sections.setdefault(ph, set()).add(s_idx)

    for ph, sec_set in phrase_to_sections.items():
        if len(sec_set) >= 3:
            sorted_secs = sorted(sec_set)
            warnings.append(
                QualityWarning(
                    code="REPEATED_DISTINCTIVE_PHRASE",
                    message=f"Distinctive phrase repeated across sections {sorted_secs}: '{ph}'",
                    severity=QualitySeverity.INFO,
                    metadata={"phrase": ph, "sections": sorted_secs, "repetition_count": len(sorted_secs)},
                )
            )

    # 6. Section budget divergence
    if outline and outline.get("sections"):
        outline_budgets = {s.get("section_index"): s.get("word_budget") for s in outline["sections"]}
        for seg in cleaned_segments:
            s_order = seg.get("order")
            tgt_budget = outline_budgets.get(s_order)
            if tgt_budget and tgt_budget >= 300:
                act_words = len(seg.get("narration", "").split())
                if act_words < int(tgt_budget * 0.35):
                    warnings.append(
                        QualityWarning(
                            code="SECTION_BUDGET_DIVERGENCE",
                            message=f"Section {s_order} severely undershot budget ({act_words} words vs {tgt_budget} target).",
                            section_index=s_order,
                            severity=QualitySeverity.WARNING,
                        )
                    )
                elif act_words > int(tgt_budget * 2.50):
                    warnings.append(
                        QualityWarning(
                            code="SECTION_BUDGET_DIVERGENCE",
                            message=f"Section {s_order} severely exceeded budget ({act_words} words vs {tgt_budget} target).",
                            section_index=s_order,
                            severity=QualitySeverity.WARNING,
                        )
                    )

    # 6. Duration divergence against requested target
    if job and getattr(job, "target_minutes", None):
        target_mins_raw = str(job.target_minutes).lower().strip()
        if target_mins_raw not in ("auto", "literal", "none"):
            try:
                target_m = float(target_mins_raw)
                dur_res = calculate_script_duration(cleaned_script, job.custom_speed or 1.0)
                pred_sec = dur_res.get("predicted_duration_seconds", 0)
                tgt_sec = target_m * 60.0
                if pred_sec < (tgt_sec * 0.70):
                    warnings.append(
                        QualityWarning(
                            code="DURATION_DIVERGENCE",
                            message=f"Predicted duration ({pred_sec // 60}m) is under 70% of requested target ({int(target_m)}m).",
                            severity=QualitySeverity.WARNING,
                        )
                    )
                elif pred_sec > (tgt_sec * 1.35):
                    warnings.append(
                        QualityWarning(
                            code="DURATION_DIVERGENCE",
                            message=f"Predicted duration ({pred_sec // 60}m) exceeds 135% of requested target ({int(target_m)}m).",
                            severity=QualitySeverity.WARNING,
                        )
                    )
            except (ValueError, TypeError):
                pass

    # 7. Unresolved fidelity findings
    audit_data = fidelity_audit or (getattr(job, "fidelity_audit_json", None) if job else None)
    if isinstance(audit_data, dict):
        has_mat = audit_data.get("has_material_issues")
        repaired = audit_data.get("repair_succeeded")
        unresolved = audit_data.get("unresolved_issue")
        if unresolved or (has_mat and not repaired):
            warnings.append(
                QualityWarning(
                    code="UNRESOLVED_FIDELITY_FINDING",
                    message="Material factual fidelity issues remain unresolved from fidelity audit.",
                    severity=QualitySeverity.CRITICAL,
                    metadata={"repair_instructions": audit_data.get("repair_instructions")},
                )
            )

    has_warn = any(w.severity in (QualitySeverity.WARNING, QualitySeverity.CRITICAL) for w in warnings)
    status = QualityStatus.WARN if has_warn else QualityStatus.PASS

    return cleaned_script, QualityReport(
        status=status,
        warnings=warnings,
        cleanups_applied=cleanups,
    )
