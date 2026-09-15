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
    r"^(?:topic|subject|title):\s*",
    r"^(?:section\s+\d+|chapter\s+\d+):\s*",
    r"^(?:heading|header):\s*",
    r"^(?:narration|script):\s*",
]

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

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


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
    cleaned_script["episode_title"] = ep_title

    # Title truncation inside a word check
    if ep_title and (ep_title.endswith("-") or re.search(r"\b(?:the|a|an|of|in|and)\s*$", ep_title, re.IGNORECASE)):
        warnings.append(
            QualityWarning(
                code="SUSPICIOUS_TITLE_TRUNCATION",
                message=f"Episode title appears truncated: '{ep_title}'",
                severity=QualitySeverity.WARNING,
            )
        )

    # 2. Check headings and narration across segments
    seen_headings: dict[str, int] = {}
    paragraphs_pool: list[tuple[int, str, set[tuple[str, str, str]]]] = []
    openings_pool: list[tuple[int, str]] = []

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
                            metadata={"section_a": idx_a, "section_b": idx_b, "similarity": round(jaccard, 2)},
                        )
                    )

    # 5. Section budget divergence
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
