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
    build_already_covered_context,
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


def test_a_topic_specific_narrative_planning():
    """Test A: Grounded research returns topic-specific narrative proposal, outline uses it directly, zero extra LLM calls."""
    narrative_plan = [
        {"heading": "Plant Genetics & CRISPR", "purpose": "Explain Cas9 editing in crop genomes", "relevant_evidence_ids": ["ev_crispr_1"]},
        {"heading": "Off-Target Mutations", "purpose": "Analyze mutation frequencies and phenotyping", "relevant_evidence_ids": ["ev_crispr_2"]},
        {"heading": "Agricultural Deployment", "purpose": "Discuss field trials and yield stability", "relevant_evidence_ids": ["ev_crispr_3"]},
    ]
    packet = {
        "topic": "CRISPR off-target mutations in plant breeding",
        "scope": "research",
        "items": [
            {"evidence_id": "ev_crispr_1", "title": "CRISPR in Plants", "snippet": "Cas9 applications in crops."},
            {"evidence_id": "ev_crispr_2", "title": "Off-Target Mutations", "snippet": "Whole genome sequencing of edited plants."},
            {"evidence_id": "ev_crispr_3", "title": "Field Trials", "snippet": "Yield impacts across test plots."},
        ],
        "narrative_plan": narrative_plan,
    }
    outline = build_episode_outline(
        topic="CRISPR off-target mutations in plant breeding",
        evidence_packet=packet,
        target_minutes="15",
        scope=EvidenceScope.RESEARCH,
    )
    headings = [s["heading"] for s in outline["sections"]]
    assert headings == ["Plant Genetics & CRISPR", "Off-Target Mutations", "Agricultural Deployment"]
    # Verify no fixed domain archetype headings
    assert not any("Mechanisms, Data & Methodology" in h or "Frontier & Unresolved Questions" in h for h in headings)
    assert outline["section_count"] == 3


def test_b_fallback_narrative_planning_on_degraded_research():
    """Test B: Degraded research produces a valid outline locally with fallback domain archetypes."""
    packet_empty = {
        "topic": "CRISPR off-target mutations in plant breeding",
        "scope": "research",
        "items": [],
    }
    outline = build_episode_outline(
        topic="CRISPR off-target mutations in plant breeding",
        evidence_packet=packet_empty,
        target_minutes="15",
        scope=EvidenceScope.RESEARCH,
    )
    assert outline is not None
    assert len(outline["sections"]) >= 2
    # In fallback mode without items or narrative plan, domain archetypes are safely utilized
    headings = [s["heading"] for s in outline["sections"]]
    assert any("Foundational" in h or "Origins" in h or "Mechanisms" in h for h in headings)


def test_c_semantic_evidence_mapping_and_validation():
    """Test C: Evidence mapped semantically, invalid IDs safely ignored, no orphaned items."""
    narrative_plan = [
        {"heading": "Roman Military System", "purpose": "Explore legions and tactical reforms", "relevant_evidence_ids": ["invalid_id_999", "ev_mil_1"]},
        {"heading": "Economic Crisis & Inflation", "purpose": "Analyze currency debasement and trade collapse", "relevant_evidence_ids": ["ev_econ_1"]},
    ]
    packet = {
        "topic": "Fall of the Western Roman Empire",
        "scope": "research",
        "items": [
            {"evidence_id": "ev_econ_1", "title": "Currency Debasement", "snippet": "Silver content dropped."},
            {"evidence_id": "ev_mil_1", "title": "Legion Shortages", "snippet": "Recruitment shortfalls in 5th century."},
            {"evidence_id": "ev_extra_1", "title": "Barbarian Federati", "snippet": "Treaties with Gothic tribes."},
        ],
        "narrative_plan": narrative_plan,
    }
    outline = build_episode_outline(
        topic="Fall of the Western Roman Empire",
        evidence_packet=packet,
        target_minutes="10",
        scope=EvidenceScope.RESEARCH,
    )
    secs = outline["sections"]
    assert len(secs) == 2
    # Section 1 should have ev_mil_1, invalid_id_999 safely ignored
    assert "invalid_id_999" not in secs[0]["relevant_evidence_ids"]
    assert "ev_mil_1" in secs[0]["relevant_evidence_ids"]
    # Section 2 has ev_econ_1
    assert "ev_econ_1" in secs[1]["relevant_evidence_ids"]
    # All items including ev_extra_1 assigned across sections (no orphaned evidence)
    all_assigned = {eid for s in secs for eid in s["relevant_evidence_ids"]}
    assert "ev_extra_1" in all_assigned
    assert "ev_mil_1" in all_assigned
    assert "ev_econ_1" in all_assigned


def test_d_anti_repetition_context_is_bounded_and_structured():
    """Test D: Anti-repetition context is compact, bounded (~200 tokens), covers headings/evidence, no full narration."""
    completed = [
        {
            "section_index": 1,
            "heading": "Origins of Roman Military",
            "purpose": "Early organization of Roman legions.",
            "narration": "Rome began with citizen soldier levies. " * 50 + "Over centuries this evolved into a professional standing force.",
            "relevant_evidence_ids": ["ev_mil_1"],
            "key_points": ["Citizen levies", "Professional standing army"],
        },
        {
            "section_index": 2,
            "heading": "The 5th Century Collapse",
            "purpose": "Examine recruitment breakdown.",
            "narration": "Recruitment broke down completely in the provinces. " * 50 + "Federates filled the ranks under autonomous chieftains.",
            "relevant_evidence_ids": ["ev_mil_2"],
            "key_points": ["Recruitment breakdown", "Federati dependence"],
        },
    ]
    ctx = build_already_covered_context(
        completed,
        current_heading="The Sack of 476",
        current_purpose="Detail the final deposition of Romulus Augustulus.",
        current_idx=3,
    )
    assert "ALREADY COVERED IN PREVIOUS SECTIONS" in ctx
    assert "Origins of Roman Military" in ctx
    assert "The 5th Century Collapse" in ctx
    assert "ev_mil_1" in ctx and "ev_mil_2" in ctx
    assert 'Previous section ended with: "Federates filled the ranks under autonomous chieftains."' in ctx
    assert "Your task for Section 3 (The Sack of 476)" in ctx
    # Ensure bounded: token/word count is small (far less than full narration)
    words = ctx.split()
    assert len(words) < 250


def test_e_quality_gate_wiring_in_production():
    """Test E: Quality gate duration, budget, and fidelity checks fire when wired."""
    from herald.services.quality_gate import run_quality_gate
    mock_job = MagicMock()
    mock_job.target_minutes = "5"  # ~750 words
    mock_job.custom_speed = 1.0

    outline = {
        "sections": [
            {"section_index": 1, "heading": "Sec 1", "word_budget": 300},
            {"section_index": 2, "heading": "Sec 2", "word_budget": 300},
        ]
    }
    # Script where section 1 heavily diverges from 300 words (800 words > 2.5x of 300)
    # and total duration diverges from 5 min target
    script = {
        "episode_title": "Test Gate",
        "segments": [
            {"order": 1, "heading": "Sec 1", "narration": " ".join(["word"] * 800)},
            {"order": 2, "heading": "Sec 2", "narration": " ".join(["word"] * 800)},
        ],
    }
    fidelity_audit = {
        "status": "unresolved_issue_remains",
        "has_material_issues": True,
        "content_warning": True,
        "repair_instructions": "Fix fact errors.",
    }
    cleaned, report = run_quality_gate(
        script,
        job=mock_job,
        outline=outline,
        fidelity_audit=fidelity_audit,
    )
    codes = [w.code for w in report.warnings]
    assert "SECTION_BUDGET_DIVERGENCE" in codes
    assert "DURATION_DIVERGENCE" in codes
    assert "UNRESOLVED_FIDELITY_FINDING" in codes


def test_f_dynamic_budget_resume_behavior():
    """Test F: Resume from section_progress_json reconstructs cumulative words, recalculates subsequent targets, does not re-generate completed sections."""
    sections_def = [
        {"section_index": 1, "heading": "Sec 1", "purpose": "P1", "word_budget": 500},
        {"section_index": 2, "heading": "Sec 2", "purpose": "P2", "word_budget": 500},
        {"section_index": 3, "heading": "Sec 3", "purpose": "P3", "word_budget": 500},
    ]
    outline = {
        "episode_title": "Resume Test",
        "target_total_words": 1500,
        "sections": sections_def,
    }
    # Sec 1 was completed already with 300 words (underfill by 200)
    completed_sec1 = {
        "section_index": 1,
        "heading": "Sec 1",
        "purpose": "P1",
        "narration": " ".join(["word"] * 300),
        "word_count": 300,
        "target_word_budget": 500,
        "completed": True,
    }
    mock_db = MagicMock()
    job = PodcastJob(
        id="job-resume",
        source_hash="h-resume",
        source_text="Source",
        content_mode="topic",
        target_minutes="10",
        outline_json=outline,
        evidence_packet_json={"topic": "T", "items": []},
        section_progress_json=[completed_sec1],
    )

    executed_sections = []

    def mock_gen_sec(job, section_info, topic, evidence_packet, previous_summary, scope, db=None):
        executed_sections.append(section_info["section_index"])
        return {
            "section_index": section_info["section_index"],
            "heading": section_info["heading"],
            "narration": " ".join(["word"] * section_info["word_budget"]),
            "word_count": section_info["word_budget"],
            "target_word_budget": section_info["word_budget"],
            "completed": True,
        }

    with patch("herald.ai.long_form.generate_single_section", side_effect=mock_gen_sec), \
         patch("herald.ai.long_form.audit_and_repair_fidelity", side_effect=lambda **kw: (kw["sections"], {"status": "clean", "has_material_issues": False})):
        res = execute_unified_long_form_pipeline(
            db=mock_db,
            job=job,
            topic="Resume Topic",
            scope=EvidenceScope.RESEARCH,
            target_minutes="10",
            source_text="Source",
        )

    assert res is not None
    # 1. Sec 1 was NOT re-executed
    assert 1 not in executed_sections
    assert executed_sections == [2, 3]
    # 2. Remaining target (1500 - 300 = 1200) was redistributed over 2 remaining sections -> 600 each
    sec2 = next(s for s in job.section_progress_json if s["section_index"] == 2)
    assert sec2["target_word_budget"] == 600
    assert len(job.section_progress_json) == 3


def test_g_underfill_recovery_uncovered_vs_no_uncovered():
    """Test G: Underfill with uncovered generates 1 extra section; without uncovered generates 0 and records warning."""
    # Subtest 1: With uncovered evidence -> exactly 1 extra section
    sections_def = [
        {"section_index": 1, "heading": "S1", "purpose": "P1", "word_budget": 500, "relevant_evidence_ids": ["ev1"]},
        {"section_index": 2, "heading": "S2", "purpose": "P2", "word_budget": 500, "relevant_evidence_ids": ["ev2"]},
    ]
    outline = {
        "episode_title": "Underfill Test",
        "target_total_words": 1200,
        "sections": sections_def,
    }
    mock_db = MagicMock()
    job = PodcastJob(
        id="job-uf-1",
        source_hash="h-uf",
        source_text="Source",
        content_mode="topic",
        target_minutes="8",
        outline_json=outline,
        evidence_packet_json={
            "topic": "T",
            "items": [
                {"evidence_id": "ev1", "snippet": "snip1"},
                {"evidence_id": "ev2", "snippet": "snip2"},
                {"evidence_id": "ev3_uncovered", "title": "Uncovered Breakthrough", "snippet": "snip3"},
            ],
        },
    )

    # Generated words total 700 (<80% of 1200 = 960)
    def mock_gen_sec(job, section_info, topic, evidence_packet, previous_summary, scope, db=None):
        return {
            "section_index": section_info["section_index"],
            "heading": section_info["heading"],
            "narration": " ".join(["word"] * 350),
            "word_count": 350,
            "target_word_budget": section_info.get("word_budget"),
            "relevant_evidence_ids": section_info.get("relevant_evidence_ids", []),
            "completed": True,
        }

    with patch("herald.ai.long_form.generate_single_section", side_effect=mock_gen_sec), \
         patch("herald.ai.long_form.audit_and_repair_fidelity", side_effect=lambda **kw: (kw["sections"], {"status": "clean", "has_material_issues": False})), \
         patch("herald.ai.long_form.record_job_diagnostic_event") as mock_diag:
        res = execute_unified_long_form_pipeline(
            db=mock_db,
            job=job,
            topic="T",
            scope=EvidenceScope.RESEARCH,
            target_minutes="8",
            source_text="Source",
        )
    # Exactly 3 segments (2 original + 1 extra)
    assert len(res.segments) == 3
    assert res.segments[2].heading == "Uncovered Breakthrough"
    # Diagnostics event recorded
    recorded_events = [c[0][3] for c in mock_diag.call_args_list]
    assert "UNCOVERED_EVIDENCE_SECTION_GENERATED" in recorded_events

    # Subtest 2: NO uncovered evidence -> 0 extra sections, warning recorded
    job2 = PodcastJob(
        id="job-uf-2",
        source_hash="h-uf2",
        source_text="Source",
        content_mode="topic",
        target_minutes="8",
        outline_json=outline,
        evidence_packet_json={
            "topic": "T",
            "items": [
                {"evidence_id": "ev1", "snippet": "snip1"},
                {"evidence_id": "ev2", "snippet": "snip2"},
            ],
        },
    )
    with patch("herald.ai.long_form.generate_single_section", side_effect=mock_gen_sec), \
         patch("herald.ai.long_form.audit_and_repair_fidelity", side_effect=lambda **kw: (kw["sections"], {"status": "clean", "has_material_issues": False})), \
         patch("herald.ai.long_form.record_job_diagnostic_event") as mock_diag2:
        res2 = execute_unified_long_form_pipeline(
            db=mock_db,
            job=job2,
            topic="T",
            scope=EvidenceScope.RESEARCH,
            target_minutes="8",
            source_text="Source",
        )
    assert len(res2.segments) == 2
    recorded_events2 = [c[0][3] for c in mock_diag2.call_args_list]
    assert "DURATION_UNDERFILL_ACCEPTED" in recorded_events2


def test_h_call_count_regression_five_sections():
    """Test H: A 5-section episode executes exactly 1 research call + 5 section calls + 0 outline calls."""
    mock_db = MagicMock()
    job = PodcastJob(
        id="job-call-count",
        source_hash="h-cc",
        source_text="Seed",
        content_mode="topic",
        target_minutes="15",
    )
    narrative_plan = [
        {"heading": f"Sec {i}", "purpose": f"Purp {i}", "relevant_evidence_ids": [f"ev_{i}"]}
        for i in range(1, 6)
    ]
    research_mock_data = {
        "topic": "5-Sec Topic",
        "search_count": 3,
        "source_count": 5,
        "narrative_plan": narrative_plan,
        "search_results": [{"title": f"Source {i}", "url": f"http://s{i}.com", "content": f"Text {i}"} for i in range(1, 6)],
        "synthesized_research": "Research body text with detailed facts.",
    }

    call_counts = {"research": 0, "section": 0, "outline": 0, "fidelity": 0}

    def mock_failover(job, operation, execute_fn, **kwargs):
        provider = MagicMock()
        if operation == "grounded_research":
            call_counts["research"] += 1
            provider.generate_grounded_research.return_value = research_mock_data
            return execute_fn(provider, 1, kwargs.get("source_text", ""))
        elif operation == "section_generation":
            call_counts["section"] += 1
            provider.generate_script.return_value = PodcastScriptResponse(
                episode_title="Title",
                episode_description="Desc",
                segments=[PodcastSegment(order=1, heading="H", narration=" ".join(["word"] * 450))],
                warnings=[],
            )
            return execute_fn(provider, 1, kwargs.get("source_text", ""))
        elif operation in ("verification", "research_audit"):
            call_counts["fidelity"] += 1
            res_audit = MagicMock()
            res_audit.has_material_issues = False
            res_audit.model_dump.return_value = {"status": "clean"}
            return res_audit
        raise AssertionError(f"Unexpected LLM operation called: {operation}")

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover):
        res = execute_unified_long_form_pipeline(
            db=mock_db,
            job=job,
            topic="5-Sec Topic",
            scope=EvidenceScope.RESEARCH,
            target_minutes="15",
            source_text="Seed",
        )

    # Exactly 1 research + 5 section calls + 0 outline calls
    assert call_counts["research"] == 1
    assert call_counts["section"] == 5
    assert call_counts["outline"] == 0
    assert len(res.segments) == 5
    total_llm_calls = call_counts["research"] + call_counts["section"] + call_counts["outline"]
    assert total_llm_calls == 6

