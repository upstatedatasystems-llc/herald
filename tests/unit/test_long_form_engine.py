"""Unit tests for Unified Long-Form Planning, Research, and Coherence Engine.
Tests:
- Word budget calculation (10m, 20m, 30m, 45m, 60m, auto)
- Coverage ledger extraction and boilerplate filtering
- Research plan depth scaling (low, medium, high)
- Source mode small-source bounding (no hallucinated filler)
- Auto mode has no hidden fixed word target
- Anti-compression guard in final assembly
"""

import pytest

from herald.ai.long_form import (
    DURATION_WORD_BUDGETS,
    EvidenceScope,
    assemble_and_smooth_script,
    build_episode_outline,
    build_research_plan,
    build_source_coverage_ledger,
    get_target_word_budget,
    normalize_evidence_packet,
)


def test_target_word_budgets():
    assert get_target_word_budget("10") == 1250
    assert get_target_word_budget(10) == 1250
    assert get_target_word_budget("20") == 2500
    assert get_target_word_budget("30") == 3750
    assert get_target_word_budget("45") == 5600
    assert get_target_word_budget("60") == 7500
    assert get_target_word_budget("auto") is None
    assert get_target_word_budget(None) is None


def test_coverage_ledger_extraction_and_boilerplate_filtering():
    raw_source = """
The United States Navy operates 22 Virginia-class fast attack submarines.
Each submarine displaces approximately 7,800 tons submerged and costs $3.45 billion.
Admiral John Richardson praised the program's stealth and reactor technology.

Subscribe to our newsletter for daily defense updates!
Click here to read more.
Copyright 2026 Defense News. All rights reserved.
Follow us on Twitter @DefenseNews.
"""
    ledger = build_source_coverage_ledger(raw_source, source_title="Virginia Subs")

    # Verify numbers extracted
    assert any("22" in n for n in ledger["key_numbers"])
    assert any("7,800" in n or "7800" in n for n in ledger["key_numbers"])
    assert any("3.45" in n for n in ledger["key_numbers"])

    # Verify proper nouns extracted
    assert any("United States" in ent or "Virginia" in ent for ent in ledger["key_entities"])

    # Verify boilerplate omitted
    assert len(ledger["omitted_pollution"]) >= 3
    assert "subscribe to our newsletter" not in ledger["clean_text"].lower()
    assert "all rights reserved" not in ledger["clean_text"].lower()


def test_research_plan_depth_scaling():
    plan_low = build_research_plan("Fusion Energy", research_depth="low", scope=EvidenceScope.RESEARCH)
    assert len(plan_low["focus_areas"]) == 2

    plan_med = build_research_plan("Fusion Energy", research_depth="medium", scope=EvidenceScope.RESEARCH)
    assert len(plan_med["focus_areas"]) == 3

    plan_high = build_research_plan("Fusion Energy", research_depth="high", scope=EvidenceScope.RESEARCH)
    assert len(plan_high["focus_areas"]) == 5


def test_source_mode_small_source_bounds_word_budget():
    # Small source ~ 60 words
    small_source = "The quick brown fox jumps over the lazy dog. " * 7
    ledger = build_source_coverage_ledger(small_source)
    packet = normalize_evidence_packet("Fox Behavior", EvidenceScope.SOURCE_ONLY, seed_source_text=small_source)

    # User requested 60 minutes (~7,500 words)
    outline = build_episode_outline(
        topic="Fox Behavior",
        evidence_packet=packet,
        target_minutes="60",
        scope=EvidenceScope.SOURCE_ONLY,
        source_ledger=ledger,
    )

    # Word budget must be bounded to avoid hallucinated padding
    assert outline["target_total_words"] < 2000
    assert outline["target_total_words"] < 7500


def test_auto_mode_has_no_fixed_word_quota():
    packet = normalize_evidence_packet("Quantum Sensors", EvidenceScope.RESEARCH, grounded_research_data={
        "research_sources": [
            {"title": "Sensor 1", "snippet": "A"},
            {"title": "Sensor 2", "snippet": "B"},
        ]
    })
    outline = build_episode_outline(
        topic="Quantum Sensors",
        evidence_packet=packet,
        target_minutes="auto",
        scope=EvidenceScope.RESEARCH,
    )
    assert outline["is_auto"] is True
    # Word budget is naturally proportioned to evidence, not forced to 1500-2500
    assert outline["section_count"] >= 3


def test_assemble_and_smooth_script_anti_compression():
    sections = [
        {"section_index": 1, "heading": "Part 1", "narration": "Word " * 500, "word_count": 500},
        {"section_index": 2, "heading": "Part 2", "narration": "Word " * 500, "word_count": 500},
        {"section_index": 3, "heading": "Part 3", "narration": "Word " * 500, "word_count": 500},
    ]
    script = assemble_and_smooth_script(
        episode_title="Test Episode",
        episode_description="Test Description",
        sections=sections,
    )
    assert len(script.segments) == 3
    total_words = sum(len(s.narration.split()) for s in script.segments)
    assert total_words >= 1400  # Within tolerance of 1500 words

    # If sections collapsed into tiny summary, anti-compression raises ValueError
    bad_sections = [
        {"section_index": 1, "heading": "Part 1", "narration": "Short summary.", "word_count": 1000},
    ]
    with pytest.raises(ValueError, match="Anti-Compression violation"):
        assemble_and_smooth_script(
            episode_title="Collapsed Episode",
            episode_description="Collapsed Description",
            sections=bad_sections,
        )
