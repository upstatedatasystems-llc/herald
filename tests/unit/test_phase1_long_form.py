"""Unit tests for Phase 1: Content Generation & Efficiency (Long-Form Engine).

Verifies:
1. Topic-specific narrative planning (not generic universal headings).
2. Spoken-writing contract in prompt instructions.
3. Single-pass section generation (no second LLM call for <80% output).
4. Dynamic remaining-budget redistribution.
5. Removal of normal generic catch-up recap ("Comprehensive Analysis and Evidence Synthesis").
"""

from unittest.mock import MagicMock, patch

from herald.ai.long_form import (
    EvidenceScope,
    build_episode_outline,
    build_research_plan,
    execute_unified_long_form_pipeline,
    generate_single_section,
)
from herald.ai.schema import PodcastScriptResponse, PodcastSegment
from herald.db.models import PodcastJob


def test_topic_specific_research_planning():
    """Verify research planning tailors focus areas to the subject domain."""
    plan_astro = build_research_plan("Black Holes and Singularity Physics", research_depth="medium")
    areas_astro = [fa["name"] for fa in plan_astro["focus_areas"]]
    # Should reflect astrophysical / scientific concepts
    assert any("Formation" in a or "Physics" in a or "Origins" in a or "Observation" in a for a in areas_astro)

    plan_biz = build_research_plan("Texas Roadhouse Restaurant Operations", research_depth="medium")
    areas_biz = [fa["name"] for fa in plan_biz["focus_areas"]]
    # Should reflect business / operational / culinary concepts
    assert any("Business" in a or "Operation" in a or "Model" in a or "Culture" in a or "Strategy" in a for a in areas_biz)
    assert areas_astro != areas_biz


def test_topic_specific_episode_outline_structure():
    """Verify outline sections contain distinct jobs, key points, anti-repetition, and budget ranges."""
    packet = {
        "topic": "Texas Roadhouse",
        "scope": "research",
        "items": [
            {"evidence_id": "ev_1", "title": "Founding & Concept", "snippet": "Founded in 1993 in Clarksville, Indiana by Kent Taylor."},
            {"evidence_id": "ev_2", "title": "Hand-Cut Steaks & Rolls", "snippet": "Fresh baked rolls with cinnamon butter, hand-cut steaks displayed in meat room."},
            {"evidence_id": "ev_3", "title": "Managing Partner Model", "snippet": "Managing partners invest $25,000 and receive 10% of store profits."},
        ],
    }
    outline = build_episode_outline(
        topic="Texas Roadhouse",
        evidence_packet=packet,
        target_minutes="15",
        scope=EvidenceScope.RESEARCH,
    )
    sections = outline["sections"]
    assert len(sections) >= 3

    for sec in sections:
        assert "section_index" in sec
        assert "heading" in sec
        assert "purpose" in sec
        assert "word_budget" in sec
        assert "word_budget_min" in sec
        assert "word_budget_max" in sec
        assert "key_points" in sec
        assert "anti_repetition" in sec
        # No generic heading
        assert sec["heading"] != "Comprehensive Analysis and Evidence Synthesis"
        assert sec["word_budget_min"] <= sec["word_budget"] <= sec["word_budget_max"]


def test_spoken_writing_contract_in_control_instructions():
    """Verify section generation prompt includes listening-first spoken writing requirements."""
    captured_instructions = []

    def mock_failover(job, operation, execute_fn, **kwargs):
        provider = MagicMock()
        def mock_gen_script(*args, **kw):
            gen_inst = kw.get("generation_instructions", "")
            captured_instructions.append(gen_inst)
            return PodcastScriptResponse(
                episode_title="Test",
                episode_description="Desc",
                segments=[PodcastSegment(order=1, heading="Sec 1", narration="Spoken prose here.")],
                warnings=[],
            )
        provider.generate_script = mock_gen_script
        return execute_fn(provider, 1, kwargs.get("source_text", ""))

    job = PodcastJob(id="job-spoken-contract", source_hash="hash-1", source_text="Source")
    sec_info = {
        "section_index": 1,
        "heading": "The Founding of Texas Roadhouse",
        "purpose": "Explain the origin story and initial diner concept.",
        "word_budget": 500,
        "word_budget_min": 425,
        "word_budget_max": 575,
        "key_points": ["Kent Taylor founded in 1993", "Clarksville Indiana"],
        "anti_repetition": "Focus purely on founding story; do not recap or jump to current earnings.",
        "relevant_evidence_ids": ["ev_1"],
    }
    packet = {
        "topic": "Texas Roadhouse",
        "items": [{"evidence_id": "ev_1", "title": "Founding", "snippet": "Kent Taylor 1993."}],
    }

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover):
        res = generate_single_section(
            job=job,
            section_info=sec_info,
            topic="Texas Roadhouse",
            evidence_packet=packet,
            previous_summary=None,
            scope=EvidenceScope.RESEARCH,
        )

    assert res["completed"] is True
    assert len(captured_instructions) == 1
    inst = captured_instructions[0]
    # Check spoken-writing requirements
    assert "conversational" in inst.lower() or "spoken" in inst.lower()
    assert "Target Word Range" in inst or "425" in inst
    assert "anti-repetition" in inst.lower() or "do not recap" in inst.lower()
    assert "Kent Taylor founded in 1993" in inst


def test_single_pass_section_generation_no_regeneration_when_underfilled():
    """Verify section <80% of budget does NOT trigger a second full-section generation request."""
    call_count = 0

    def mock_failover(job, operation, execute_fn, **kwargs):
        nonlocal call_count
        call_count += 1
        provider = MagicMock()
        # Returns only 100 words when budget is 500 words (20% of budget, normally <80%)
        mock_narr = " ".join(["word"] * 100)
        provider.generate_script.return_value = PodcastScriptResponse(
            episode_title="Test",
            episode_description="Desc",
            segments=[PodcastSegment(order=1, heading="Sec 1", narration=mock_narr)],
            warnings=[],
        )
        return execute_fn(provider, 1, kwargs.get("source_text", ""))

    job = PodcastJob(id="job-single-pass", source_hash="hash-sp", source_text="Source")
    sec_info = {
        "section_index": 1,
        "heading": "Heading 1",
        "purpose": "Purpose 1",
        "word_budget": 500,
        "word_budget_min": 400,
        "word_budget_max": 600,
    }
    packet = {"topic": "Topic", "items": []}

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover):
        res = generate_single_section(
            job=job,
            section_info=sec_info,
            topic="Topic",
            evidence_packet=packet,
            previous_summary=None,
            scope=EvidenceScope.RESEARCH,
        )

    # CRITICAL: exactly ONE generation call must be made! No regeneration!
    assert call_count == 1
    assert res["word_count"] == 100
    assert res["section_index"] == 1


def test_dynamic_word_budgeting_redistributes_remaining_target():
    """Verify that earlier underfill or overfill dynamically adjusts subsequent section targets."""
    # Setup 3-section outline with total target 1500 words (500 words each nominal)
    sections_def = [
        {"section_index": 1, "heading": "S1", "purpose": "P1", "word_budget": 500, "word_budget_min": 425, "word_budget_max": 575},
        {"section_index": 2, "heading": "S2", "purpose": "P2", "word_budget": 500, "word_budget_min": 425, "word_budget_max": 575},
        {"section_index": 3, "heading": "S3", "purpose": "P3", "word_budget": 500, "word_budget_min": 425, "word_budget_max": 575},
    ]

    outline = {
        "episode_title": "Test",
        "episode_description": "Desc",
        "target_total_words": 1500,
        "sections": sections_def,
    }

    mock_db = MagicMock()
    job = PodcastJob(
        id="job-dyn-budget",
        source_hash="h1",
        source_text="Source",
        content_mode="topic",
        target_minutes="10",
        outline_json=outline,
        evidence_packet_json={"topic": "T", "items": []},
    )

    generated_word_counts = [250, 600, 650]  # S1 underfills by 250 words
    current_sec_idx = 0
    actual_budgets_passed = []

    def mock_gen_sec(job, section_info, topic, evidence_packet, previous_summary, scope, db=None):
        nonlocal current_sec_idx
        actual_budgets_passed.append(section_info.get("word_budget"))
        wc = generated_word_counts[current_sec_idx]
        current_sec_idx += 1
        return {
            "section_index": section_info["section_index"],
            "heading": section_info["heading"],
            "narration": " ".join(["word"] * wc),
            "word_count": wc,
            "target_word_budget": section_info.get("word_budget"),
            "completed": True,
        }

    with patch("herald.ai.long_form.generate_single_section", side_effect=mock_gen_sec), \
         patch("herald.ai.long_form.audit_and_repair_fidelity", side_effect=lambda **kw: (kw["sections"], {"status": "clean", "has_material_issues": False})):
        res = execute_unified_long_form_pipeline(
            db=mock_db,
            job=job,
            topic="Test Topic",
            scope=EvidenceScope.RESEARCH,
            target_minutes="10",
            source_text="Source",
        )

    # Section 1 started at 500. Generated 250 (shortfall 250).
    # Remaining target was 1500 - 250 = 1250 across 2 remaining sections -> 625 each!
    assert res is not None
    assert actual_budgets_passed[0] == 500
    assert actual_budgets_passed[1] == 625  # Increased!


def test_remove_generic_catchup_section():
    """Verify completion does NOT inject 'Comprehensive Analysis and Evidence Synthesis' generic section."""
    sections_def = [
        {"section_index": 1, "heading": "S1", "purpose": "P1", "word_budget": 500},
        {"section_index": 2, "heading": "S2", "purpose": "P2", "word_budget": 500},
    ]

    outline = {
        "episode_title": "Test",
        "episode_description": "Desc",
        "target_total_words": 1000,
        "sections": sections_def,
    }

    mock_db = MagicMock()
    job = PodcastJob(
        id="job-no-catchup",
        source_hash="h1",
        source_text="Source",
        content_mode="topic",
        target_minutes="8",
        outline_json=outline,
        evidence_packet_json={"topic": "T", "items": [{"evidence_id": "ev1", "snippet": "snip"}]},
    )

    # Generated words total 700 (<80% of 1000)
    def mock_gen_sec(job, section_info, topic, evidence_packet, previous_summary, scope, db=None):
        return {
            "section_index": section_info["section_index"],
            "heading": section_info["heading"],
            "narration": " ".join(["word"] * 350),
            "word_count": 350,
            "target_word_budget": section_info.get("word_budget"),
            "completed": True,
        }

    with patch("herald.ai.long_form.generate_single_section", side_effect=mock_gen_sec), \
         patch("herald.ai.long_form.audit_and_repair_fidelity", side_effect=lambda **kw: (kw["sections"], {"status": "clean", "has_material_issues": False})):
        res = execute_unified_long_form_pipeline(
            db=mock_db,
            job=job,
            topic="Test Topic",
            scope=EvidenceScope.RESEARCH,
            target_minutes="8",
            source_text="Source",
        )

    # Must NOT have generic catch-up section
    headings = [seg.heading for seg in res.segments]
    assert "Comprehensive Analysis and Evidence Synthesis" not in headings
