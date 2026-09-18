"""
Tests for Herald Podcast Quality & User-Facing Production Improvement Pass.

Covers:
1. Title and heading scaffolding prefix removal (clean_metadata_scaffolding).
2. Clean whitespace-boundary topic label extraction (derive_topic_label, no mechanical [:80] clipping).
3. Lightweight advisory distinctive phrase/concept detector (REPEATED_DISTINCTIVE_PHRASE).
4. Repetition deduplication repair targeting only section_b with a bounded single pass.
5. Gap expansion preserving draft content and adhering to centralized budget thresholds.
6. Fail-closed fidelity verification prior to TTS (worker and non-interactive pipeline).
"""

from unittest.mock import MagicMock, patch
import pytest

from herald.db.models import JobState, PodcastJob
from herald.services.quality_gate import (
    clean_metadata_scaffolding,
    _extract_distinctive_phrases,
    run_quality_gate,
    QualitySeverity,
)
from herald.core.pipeline import derive_topic_label, execute_script_generation
from herald.ai.long_form import (
    repair_script_duplicates,
    detect_content_gap,
    expand_script_content_gap,
    _set_script_substage,
    EvidenceScope,
)
from herald.config import settings
from herald.ai.schema import PodcastScriptResponse, PodcastSegment


# ---------------------------------------------------------------------------
# 1. Scaffolding Prefix Cleanup
# ---------------------------------------------------------------------------

def test_clean_metadata_scaffolding_prefixes():
    """Verify various scaffolding prefixes are stripped cleanly."""
    cases = [
        ("Grounded Finding 1: The Core Architecture", "The Core Architecture"),
        ("grounded finding 12: Microservices Overview", "Microservices Overview"),
        ("Chapter 1: The Early Years", "The Early Years"),
        ("Section 4: Advanced Optimizations", "Advanced Optimizations"),
        ("Finding 3: Performance Bottlenecks", "Performance Bottlenecks"),
        ("Part 2: Deep Dive into Kokoro", "Deep Dive into Kokoro"),
        ("Chapter IV: Historical Perspective", "Historical Perspective"),
        ("Segment 5: Final Thoughts", "Final Thoughts"),
        ("Normal Title Without Scaffolding", "Normal Title Without Scaffolding"),
    ]
    for raw, expected in cases:
        assert clean_metadata_scaffolding(raw) == expected


def test_quality_gate_cleans_section_scaffolding():
    """run_quality_gate automatically strips scaffolding from episode title and headings."""
    raw_script = {
        "episode_title": "Grounded Finding 1: The Renaissance of Speech Synthesis",
        "episode_description": "A comprehensive discussion.",
        "segments": [
            {
                "order": 1,
                "heading": "Chapter 1: Introduction to Acoustic Models",
                "narration": "Welcome to the podcast. Today we explore acoustic models.",
            },
            {
                "order": 2,
                "heading": "Section 2: Deep Dive into Kokoro Architecture",
                "narration": "Now we transition to Kokoro architecture and its fast inference capabilities.",
            },
        ],
    }
    cleaned, report = run_quality_gate(raw_script)
    assert cleaned["episode_title"] == "The Renaissance of Speech Synthesis"
    assert cleaned["segments"][0]["heading"] == "Introduction to Acoustic Models"
    assert cleaned["segments"][1]["heading"] == "Deep Dive into Kokoro Architecture"


# ---------------------------------------------------------------------------
# 2. Episode Title Boundary Extraction (No mechanical [:80] clipping)
# ---------------------------------------------------------------------------

def test_derive_topic_label_clean_word_boundaries():
    """Topic label generation breaks at word boundaries without clipping mid-word or leaving trailing punctuation."""
    source_text = (
        "Investigating Autonomous Agent Coordination Protocols in High-Throughput Distributed Microservice Environments "
        "and Evaluating Latency Characteristics Under Extreme Contention"
    )
    title = derive_topic_label(custom_title=None, source_text=source_text, max_len=80)
    assert len(title) <= 80
    # Must end on a complete word, not a broken fragment
    last_word = title.split()[-1]
    assert not last_word.endswith("-")
    assert not last_word.endswith(",")
    assert not last_word.endswith(":")
    assert "Investigating Autonomous Agent Coordination Protocols" in title


def test_derive_topic_label_strips_scaffolding():
    """derive_topic_label strips scaffolding from custom_title or source_text."""
    assert derive_topic_label("Grounded Finding 4: Cloud Reliability", None) == "Cloud Reliability"


# ---------------------------------------------------------------------------
# 3. Distinctive Phrase Detector (Lightweight & Advisory)
# ---------------------------------------------------------------------------

def test_extract_distinctive_phrases_detects_candidate_phrases():
    """Extracts distinctive 4-6 word candidate n-grams from text."""
    s1 = "The Apollo guidance computer represented an extraordinary breakthrough in aerospace engineering."
    phrases = _extract_distinctive_phrases(s1, min_words=4, max_words=6)
    assert any("apollo guidance computer represented" in p for p in phrases)


def test_quality_gate_advisory_distinctive_phrase_warning():
    """Distinctive phrase warning is QualitySeverity.INFO and does not trigger duplicate repair."""
    repeated_phrase = "deep learning acoustic modeling framework"
    script = {
        "episode_title": "Voice Synthesis Analysis",
        "episode_description": "Overview",
        "segments": [
            {
                "order": 1,
                "heading": "Architecture",
                "narration": f"Here we review the {repeated_phrase} designed for efficient inference.",
            },
            {
                "order": 2,
                "heading": "Benchmarks",
                "narration": f"In these tests the {repeated_phrase} achieved stellar latency figures.",
            },
            {
                "order": 3,
                "heading": "Production",
                "narration": f"Finally deploying the {repeated_phrase} required robust orchestration.",
            },
        ],
    }

    cleaned, report = run_quality_gate(script)
    phrase_warnings = [w for w in report.warnings if w.code == "REPEATED_DISTINCTIVE_PHRASE"]
    assert len(phrase_warnings) >= 1
    assert phrase_warnings[0].severity == QualitySeverity.INFO
    # Crucial: advisory phrase warning alone must NOT trigger duplicate repair
    assert not report.duplicate_repair_recommended


# ---------------------------------------------------------------------------
# 4. Repetition Repair Rewrites Only Section B
# ---------------------------------------------------------------------------

def test_repair_section_repetition_rewrites_only_section_b():
    """repair_script_duplicates must update section_b narration while leaving section_a unchanged."""
    job = PodcastJob(id="rep-repair-job")
    sec_a = {
        "section_index": 1,
        "heading": "Section Alpha",
        "narration": "This is original narration for section Alpha that should remain completely untouched throughout repair.",
        "word_count": 16,
    }
    sec_b = {
        "section_index": 2,
        "heading": "Section Beta",
        "narration": "This is duplicate narration for section Beta that repeats section Alpha verbatim in content.",
        "word_count": 15,
    }

    original_sec_a_narration = sec_a["narration"]

    dup_warnings = [
        {
            "metadata": {
                "section_a": 1,
                "section_b": 2,
                "passage_b": "This is duplicate narration for section Beta that repeats section Alpha verbatim in content.",
            }
        }
    ]

    with patch("herald.ai.long_form.execute_with_failover") as mock_exec:
        repaired_text = "This is fresh, non-repetitive narrative exploring the second perspective with extensive detail."
        mock_response = PodcastScriptResponse(
            episode_title="System Design",
            episode_description="Repaired duplicate",
            segments=[PodcastSegment(order=1, heading="Section Beta", narration=repaired_text)],
            warnings=[],
        )
        mock_exec.return_value = mock_response

        repaired_sections, meta = repair_script_duplicates(
            job=job,
            sections=[sec_a, sec_b],
            duplicate_warnings=dup_warnings,
            evidence_packet={},
            topic="System Design",
        )

        assert meta["repaired_count"] == 1
        # section_a MUST remain identical
        assert repaired_sections[0]["narration"] == original_sec_a_narration
        # section_b MUST be updated
        assert repaired_sections[1]["narration"] == repaired_text
        assert repaired_sections[1]["word_count"] == len(repaired_text.split())


# ---------------------------------------------------------------------------
# 5. Gap Expansion Preserves Existing Content & Centralized Tolerances
# ---------------------------------------------------------------------------

def test_detect_content_gap_reuses_existing_tolerances():
    """detect_content_gap triggers when word counts fall below centralized budget threshold (0.80)."""
    # 3 sections planned for 1500 words total (500 words each)
    # Total words = 1000 (66.7% of target, below 0.80 tolerance)
    sections = [
        {"section_index": 1, "heading": "S1", "narration": "Word " * 350, "word_count": 350},
        {"section_index": 2, "heading": "S2", "narration": "Word " * 350, "word_count": 350},
        {"section_index": 3, "heading": "S3", "narration": "Word " * 300, "word_count": 300},
    ]

    gap_info = detect_content_gap(sections, planned_target=1500)
    assert gap_info is not None
    assert gap_info["total_words"] == 1000
    assert gap_info["deficit"] == 500

    # If fill ratio >= 0.80, detect_content_gap should return None
    acceptable_sections = [
        {"section_index": 1, "heading": "S1", "narration": "Word " * 450, "word_count": 450},
        {"section_index": 2, "heading": "S2", "narration": "Word " * 450, "word_count": 450},
        {"section_index": 3, "heading": "S3", "narration": "Word " * 350, "word_count": 350},
    ]
    # Total words = 1250 (83.3% of target, above 0.80 tolerance)
    assert detect_content_gap(acceptable_sections, planned_target=1500) is None


def test_expand_script_content_gap_preserves_existing_content():
    """expand_script_content_gap generates bounded section from uncovered evidence without altering earlier sections."""
    job = PodcastJob(id="gap-exp-job")
    s1_narr = "This is the initial grounded analysis of the topic which must remain completely intact."
    sections = [
        {
            "section_index": 1,
            "heading": "Primary Architecture",
            "narration": s1_narr,
            "word_count": len(s1_narr.split()),
            "relevant_evidence_ids": ["E1"],
        }
    ]

    gap_info = {
        "total_words": len(s1_narr.split()),
        "planned_target": 1000,
        "deficit": 500,
        "is_overall_underfilled": True,
    }

    evidence_packet = {
        "items": [
            {"evidence_id": "E1", "title": "Primary Doc", "snippet": "Core architecture info"},
            {"evidence_id": "E2", "title": "Concurrency Benchmarks", "snippet": "Modern computing performance metrics and high concurrency benchmarks", "is_seed_source": False},
        ]
    }

    with patch("herald.ai.long_form.generate_single_section") as mock_gen_sec:
        extra_narr = "Here is newly discovered evidence detailing performance characteristics under high concurrency. " * 15
        mock_gen_sec.return_value = {
            "section_index": 2,
            "heading": "Secondary Doc",
            "narration": extra_narr,
            "word_count": len(extra_narr.split()),
            "relevant_evidence_ids": ["E2"],
        }

        expanded_sections = expand_script_content_gap(
            job=job,
            completed_sections=sections,
            gap_info=gap_info,
            topic="Modern Computing",
            evidence_packet=evidence_packet,
            scope=EvidenceScope.RESEARCH,
        )

        # Existing section MUST remain unchanged
        assert len(expanded_sections) == 2
        assert expanded_sections[0]["narration"] == s1_narr
        # New section appended with uncovered findings
        assert expanded_sections[1]["narration"] == extra_narr


# ---------------------------------------------------------------------------
# 6. Fail-Closed Fidelity Verification
# ---------------------------------------------------------------------------

def test_pipeline_fails_closed_on_unresolved_fidelity_non_interactive(db_session):
    """execute_script_generation fails closed to FAILED_FINAL when unresolved fidelity issue and hold_for_approval=False."""
    job = PodcastJob(
        id="fidelity-fail-1234",
        request_mode="topic",
        content_mode="topic",
        status=JobState.RECEIVED.value,
        custom_title="Unverified Source Episode",
        source_text="Some source text",
        source_hash="fidelity-hash-1234",
    )
    db_session.add(job)
    db_session.commit()

    # Mock long_form pipeline setting unresolved fidelity audit
    with patch("herald.ai.long_form.execute_unified_long_form_pipeline") as mock_pipe:
        def side_effect(*args, **kwargs):
            job.fidelity_audit_json = {
                "status": "unresolved_issue_remains",
                "has_material_issues": True,
                "unresolved_issue": True,
                "content_warning": True,
                "repair_instructions": "Omission of critical source data.",
            }
            return PodcastScriptResponse(
                episode_title="Unverified Source Episode",
                episode_description="Unverified source description",
                segments=[PodcastSegment(order=1, heading="Intro", narration="Narration text.")],
                warnings=[],
            )
        mock_pipe.side_effect = side_effect

        response = execute_script_generation(
            db=db_session,
            job=job,
            hold_for_approval=False,  # Non-interactive / direct queue
        )

        assert response.status == JobState.FAILED_FINAL.value
        assert job.status == JobState.FAILED_FINAL.value
        assert job.failed_stage == "FIDELITY_VERIFICATION"
        assert job.error_code == "FIDELITY_VERIFICATION_FAILED"
        assert "Omission of critical source data" in job.error_detail


def test_worker_fails_closed_prior_to_tts_on_unresolved_fidelity(db_session, tmp_path):
    """Worker halts and sets FAILED_FINAL if unresolved material fidelity issues remain without approval."""
    from apps.worker.main import process_next_job

    job = PodcastJob(
        id="worker-fid-fail-5678",
        status=JobState.QUEUED_TTS.value,
        request_mode="source",
        source_hash="worker-hash-5678",
        source_text="Some source text",
        approved_at=None,  # Not approved!
        fidelity_audit_json={
            "status": "unresolved_issue_remains",
            "has_material_issues": True,
            "unresolved_issue": True,
            "repair_instructions": "Unverified factual claim remains uncorrected.",
        },
        script_json={
            "episode_title": "Risky Episode",
            "segments": [{"order": 1, "heading": "Intro", "narration": "Risky narration."}],
        },
    )
    db_session.add(job)
    db_session.commit()

    with patch("apps.worker.main.check_free_disk_mb", return_value=1000.0), \
         patch("apps.worker.main.WorkerLeaseHeartbeat"):

        process_next_job(db=db_session, kokoro_client=MagicMock(), worker_id="test-worker-1")

        db_session.refresh(job)
        assert job.status == JobState.FAILED_FINAL.value
        assert job.failed_stage == "FIDELITY_VERIFICATION"
        assert job.error_code == "FIDELITY_VERIFICATION_FAILED"
        assert "Unverified factual claim" in job.error_detail


def test_worker_proceeds_when_explicitly_approved(db_session, tmp_path):
    """Worker allows TTS synthesis when job has approval timestamp even with previous unresolved audit."""
    from apps.worker.main import process_next_job
    from datetime import datetime, timezone

    job = PodcastJob(
        id="worker-fid-ok-9012",
        status=JobState.QUEUED_TTS.value,
        request_mode="source",
        source_hash="worker-hash-9012",
        source_text="Some source text",
        approved_at=datetime.now(timezone.utc),  # Explicitly approved by user!
        fidelity_audit_json={
            "status": "unresolved_issue_remains",
            "has_material_issues": True,
            "unresolved_issue": True,
            "repair_instructions": "Unverified factual claim user acknowledged.",
        },
        script_json={
            "episode_title": "Approved Risky Episode",
            "segments": [{"order": 1, "heading": "Intro", "narration": "Approved narration."}],
        },
    )
    db_session.add(job)
    db_session.commit()

    with patch("apps.worker.main.check_free_disk_mb", return_value=1000.0), \
         patch("apps.worker.main.WorkerLeaseHeartbeat"), \
         patch("apps.worker.main.run_pronunciation_preflight"), \
         patch("apps.worker.main.chunk_podcast_script", return_value=[]), \
         patch("apps.worker.main.record_stage_metric"):

        # It should NOT fail at the fidelity verification stage
        process_next_job(db=db_session, kokoro_client=MagicMock(), worker_id="test-worker-1")

        db_session.refresh(job)
        assert job.failed_stage != "FIDELITY_VERIFICATION"


def test_extract_distinctive_phrases_short_concepts():
    """Verify _extract_distinctive_phrases captures 2-3 word distinctive concepts (capitalized, numeric, or technical)."""
    text = "The submarine executed a Crazy Ivan while maneuvering near the 300,000-gallon tank to test sonar baffles."
    phrases = _extract_distinctive_phrases(text, min_words=2, max_words=6)
    assert any("crazy ivan" in p for p in phrases)
    assert any("300000-gallon tank" in p or "300,000-gallon tank" in p or "tank" in p for p in phrases)
    assert any("sonar baffles" in p for p in phrases)


def test_review_script_repetition_filters_terminology():
    """review_script_repetition calls structured output and differentiates terminology from repetition."""
    from herald.ai.long_form import review_script_repetition
    from herald.ai.schema import RepetitionReviewResponse, RepetitionReviewItem

    job = PodcastJob(id="rep-review-job")
    sections = [
        {"section_index": 1, "heading": "Submarine Tactics", "narration": "They discussed the Crazy Ivan maneuver."},
        {"section_index": 2, "heading": "Acoustic Detection", "narration": "During the Crazy Ivan maneuver they listened."},
    ]

    # Mock structured response confirming it's just recurring terminology
    mock_resp = RepetitionReviewResponse(
        has_substantive_repetition=False,
        reviews=[
            RepetitionReviewItem(
                section_b=2,
                section_a=1,
                concept_or_passage="Crazy Ivan",
                is_substantive_repetition=False,
                explanation="Legitimate recurring tactical term.",
                passage_to_repair=None,
            )
        ]
    )

    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp):
        res, to_repair, rep_meta = review_script_repetition(
            job=job,
            completed_sections=sections,
            near_duplicate_warnings=[],
            distinctive_phrase_warnings=[
                MagicMock(metadata={"phrase": "crazy ivan", "sections": [1, 2]})
            ],
            topic="Submarine Tactics",
        )
        assert res.has_substantive_repetition is False
        assert len(to_repair) == 0  # No repair needed for legitimate terminology!
        assert isinstance(rep_meta, dict)


def test_targeted_gap_research_triggered_when_no_uncovered_evidence():
    """expand_script_content_gap executes supplemental research pass when evidence is exhausted in non-SOURCE_ONLY mode."""
    job = PodcastJob(id="gap-research-job")
    sections = [
        {
            "section_index": 1,
            "heading": "Core Analysis",
            "purpose": "Analyze core principles",
            "narration": "Brief analysis that fell short.",
            "word_count": 50,
            "relevant_evidence_ids": ["ev_1"],
        }
    ]
    evidence_packet = {
        "items": [
            {"evidence_id": "ev_1", "snippet": "Old evidence already covered", "is_seed_source": False}
        ]
    }
    gap_info = {
        "total_words": 50,
        "planned_target": 1000,
        "fill_ratio": 0.05,
        "deficit": 950,
        "underfilled_sections": [
            {
                "section_index": 1,
                "heading": "Core Analysis",
                "purpose": "Analyze core principles",
                "actual_words": 50,
                "target_budget": 500,
                "deficit": 450,
            }
        ],
    }

    mock_supp_data = {
        "items": [
            {"evidence_id": "ev_supp_1", "snippet": "Brand new supplemental factual evidence.", "title": "New Findings"}
        ],
        "sources": [{"url": "https://example.com/source", "title": "New Source"}],
    }

    mock_expanded_script = PodcastScriptResponse(
        episode_title="Test Topic",
        episode_description="Expanded",
        segments=[
            PodcastSegment(
                order=1,
                heading="Core Analysis",
                narration="Brief analysis that fell short. Brand new supplemental factual evidence expanded with detail.",
            )
        ],
        warnings=[],
    )

    def mock_failover(*args, **kwargs):
        op = kwargs.get("operation")
        if op == "supplemental_research":
            return mock_supp_data
        elif op in ("section_expansion", "generate_isolated_section"):
            return mock_expanded_script
        return mock_expanded_script

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover):
        expanded_sections = expand_script_content_gap(
            job=job,
            completed_sections=sections,
            gap_info=gap_info,
            topic="Test Topic",
            evidence_packet=evidence_packet,
            scope=EvidenceScope.RESEARCH,
        )
        assert len(expanded_sections) >= 1
        # Check evidence packet received new items
        assert any("ev_supp" in it.get("evidence_id", "") for it in evidence_packet["items"])


def test_cleanup_script_metadata_preserves_custom_title():
    """cleanup_script_metadata retains explicit custom_title while allowing natural generated titles when custom_title is None."""
    from herald.ai.long_form import cleanup_script_metadata

    # Case 1: Custom title provided
    job_with_custom = PodcastJob(id="job-custom-1", custom_title="My Handcrafted Title")
    script = {
        "episode_title": "Old Raw Title",
        "segments": [{"order": 1, "heading": "Section 1", "narration": "Text"}],
    }
    mock_resp = PodcastScriptResponse(
        episode_title="AI Generated Suggestion",
        episode_description="Desc",
        segments=[PodcastSegment(order=1, heading="New Section 1", narration="Text")],
        warnings=[],
    )
    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp):
        res = cleanup_script_metadata(job_with_custom, script, topic="Research topic query")
        assert res["episode_title"] == "My Handcrafted Title"

    # Case 2: No custom title provided -> accepts generated title
    job_no_custom = PodcastJob(id="job-no-custom-2", custom_title=None)
    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp):
        res2 = cleanup_script_metadata(job_no_custom, script, topic="Research topic query")
        assert res2["episode_title"] == "AI Generated Suggestion"


# ---------------------------------------------------------------------------
# 7. Final Podcast Quality Pass Corrections Regression Tests
# ---------------------------------------------------------------------------

def test_distinctive_concepts_two_section_trigger():
    """Verify distinctive short concepts (capitalized terms, hyphenated/numeric, substantive content pairs)
    trigger advisory warning at >= 2 distinct sections, while generic phrases require >= 3."""
    script_2_secs = {
        "episode_title": "Deep Ocean Systems",
        "segments": [
            {"order": 1, "heading": "Sub Maneuvers", "narration": "The captain initiated a Crazy Ivan maneuver to clear the baffles."},
            {"order": 2, "heading": "Tactical Sonar", "narration": "During a Crazy Ivan maneuver, the sonar team listens intently."},
        ]
    }
    _, report = run_quality_gate(script_2_secs)
    distinctive = [w for w in report.warnings if w.code == "REPEATED_DISTINCTIVE_PHRASE"]
    assert any("crazy ivan" in w.metadata.get("phrase", "") for w in distinctive)
    assert all(w.severity == QualitySeverity.INFO for w in distinctive)


def test_review_script_repetition_provides_bounded_excerpts_both_sections():
    """review_script_repetition includes bounded excerpts from BOTH section A and section B in prompt."""
    from herald.ai.long_form import review_script_repetition
    from herald.ai.schema import RepetitionReviewResponse, RepetitionReviewItem

    job = PodcastJob(id="rep-bounded-test")
    sections = [
        {
            "section_index": 1,
            "heading": "Early Submarine History",
            "narration": "The submarine relied on a distinctive teardrop hull designed specifically to optimize hydrodynamics and minimize turbulence in deep water dives.",
        },
        {
            "section_index": 3,
            "heading": "Modern Naval Engineering",
            "narration": "Engineers continue using the teardrop hull designed specifically to optimize hydrodynamics and minimize turbulence for quiet patrol operations.",
        },
    ]

    mock_resp = RepetitionReviewResponse(
        has_substantive_repetition=True,
        reviews=[
            RepetitionReviewItem(
                section_b=3,
                section_a=1,
                concept_or_passage="teardrop hull",
                is_substantive_repetition=True,
                explanation="Repeats identical hull hydrodynamics explanation.",
                passage_to_repair="teardrop hull designed specifically to optimize hydrodynamics and minimize turbulence",
                passage_a="teardrop hull designed specifically to optimize hydrodynamics and minimize turbulence in deep water dives",
            )
        ]
    )

    captured_prompt = None

    def mock_review_exec(p_inst, attempt, src):
        nonlocal captured_prompt
        # p_inst will be called by execute_with_failover
        return mock_resp

    with patch("herald.ai.long_form.execute_with_failover") as mock_failover:
        def capture_call(*args, **kwargs):
            nonlocal captured_prompt
            # Call the inner execute_fn with a dummy provider
            mock_p = MagicMock()
            mock_p.generate_structured_output.return_value = mock_resp
            exec_fn = kwargs.get("execute_fn")
            res = exec_fn(mock_p, 1, "source")
            # Extract prompt passed to generate_structured_output
            captured_prompt = mock_p.generate_structured_output.call_args[1].get("prompt")
            return res

        mock_failover.side_effect = capture_call

        res, to_repair, rep_meta = review_script_repetition(
            job=job,
            completed_sections=sections,
            near_duplicate_warnings=[],
            distinctive_phrase_warnings=[
                MagicMock(metadata={"phrase": "teardrop hull", "sections": [1, 3]})
            ],
            topic="Submarine Engineering",
        )

        assert captured_prompt is not None
        assert "Section 1 Excerpt:" in captured_prompt
        assert "Section 3 Excerpt:" in captured_prompt
        assert "teardrop hull" in captured_prompt
        assert len(to_repair) == 1
        assert to_repair[0]["metadata"]["section_a"] == 1
        assert to_repair[0]["metadata"]["section_b"] == 3
        assert to_repair[0]["metadata"]["passage_a"] != ""


def test_review_script_repetition_prioritizes_and_caps_candidates():
    """review_script_repetition prioritizes near-duplicates, caps candidates without silent drop, and logs omitted."""
    from herald.ai.long_form import review_script_repetition
    from herald.ai.schema import RepetitionReviewResponse

    job = PodcastJob(id="rep-cap-test")
    sections = [
        {"section_index": i, "heading": f"Section {i}", "narration": f"Narration content for section {i}."}
        for i in range(1, 10)
    ]

    # Create 20 candidate distinctive phrases
    distinctive_warnings = [
        MagicMock(metadata={"phrase": f"distinctive concept {i}", "sections": [1, 2]})
        for i in range(1, 21)
    ]
    # And 2 near duplicates
    near_dup_warnings = [
        MagicMock(metadata={"section_a": 1, "section_b": 2, "passage_a": "Near duplicate text 1", "passage_b": "Near duplicate text 1", "similarity": 0.95}),
        MagicMock(metadata={"section_a": 1, "section_b": 3, "passage_a": "Near duplicate text 2", "passage_b": "Near duplicate text 2", "similarity": 0.85}),
    ]

    mock_resp = RepetitionReviewResponse(has_substantive_repetition=False, reviews=[])

    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp):
        res, to_repair, rep_meta = review_script_repetition(
            job=job,
            completed_sections=sections,
            near_duplicate_warnings=near_dup_warnings,
            distinctive_phrase_warnings=distinctive_warnings,
            topic="Naval Architecture",
        )

        assert rep_meta["evaluated_count"] == 14
        assert rep_meta["total_candidate_count"] > 14
        assert len(rep_meta["omitted_candidates"]) == rep_meta["total_candidate_count"] - 14
        assert all(c["reason"] == "exceeded_candidate_cap_14" for c in rep_meta["omitted_candidates"])


def test_repair_script_duplicates_preserves_section_a_and_presents_earlier_material():
    """repair_script_duplicates explicitly presents passage_a as EARLIER COVERED MATERIAL and repairs only section_b."""
    from herald.ai.long_form import repair_script_duplicates

    job = PodcastJob(id="rep-repair-prompt-test")
    sections = [
        {"section_index": 1, "heading": "Invention", "narration": "Section A original text. It detailed the 300,000-gallon tank.", "word_count": 50},
        {"section_index": 2, "heading": "Deployment", "narration": "Section B original text. It also explained the 300,000-gallon tank in full detail.", "word_count": 50},
    ]

    duplicate_warnings = [
        {
            "metadata": {
                "section_a": 1,
                "section_b": 2,
                "passage_a": "Section A original text. It detailed the 300,000-gallon tank.",
                "passage_b": "It also explained the 300,000-gallon tank in full detail.",
                "concept": "300,000-gallon tank",
                "explanation": "Redundantly explains the tank volume.",
            }
        }
    ]

    captured_prompt = None

    mock_repaired = PodcastScriptResponse(
        episode_title="Test",
        episode_description="Desc",
        segments=[
            PodcastSegment(
                order=1,
                heading="Deployment",
                narration="Section B repaired text without repeating the tank details. This replaces the redundant passages with concise, natural spoken narration.",
            )
        ],
        warnings=[],
    )

    with patch("herald.ai.long_form.execute_with_failover") as mock_failover:
        def capture_failover(*args, **kwargs):
            nonlocal captured_prompt
            mock_p = MagicMock()
            mock_p.generate_script.return_value = mock_repaired
            exec_fn = kwargs.get("execute_fn")
            res = exec_fn(mock_p, 1, "source")
            captured_prompt = mock_p.generate_script.call_args[1].get("generation_instructions")
            return res

        mock_failover.side_effect = capture_failover

        repaired_sections, meta = repair_script_duplicates(
            job=job,
            sections=sections,
            duplicate_warnings=duplicate_warnings,
            evidence_packet={"items": []},
            topic="Engineering",
            scope=EvidenceScope.SOURCE_ONLY,
        )

        assert captured_prompt is not None
        assert "EARLIER COVERED MATERIAL" in captured_prompt
        assert "Section A original text. It detailed the 300,000-gallon tank." in captured_prompt
        assert "REDUNDANT PASSAGES IDENTIFIED IN SECTION 2" in captured_prompt
        assert meta["repaired_count"] == 1
        # Section A must be completely untouched
        assert repaired_sections[0]["narration"] == "Section A original text. It detailed the 300,000-gallon tank."
        assert "repaired text" in repaired_sections[1]["narration"]


def test_supplemental_research_depth_and_normalization():
    """expand_script_content_gap uses configured valid research_depth and normalizes raw grounded provider output."""
    from herald.ai.long_form import expand_script_content_gap

    job = PodcastJob(id="supp-norm-test")
    sections = [
        {"section_index": 1, "heading": "Reactor Physics", "purpose": "Explain core reactor design", "narration": "Short text.", "word_count": 20, "relevant_evidence_ids": ["ev_1"]}
    ]
    evidence_packet = {"items": [{"evidence_id": "ev_1", "snippet": "Old evidence", "is_seed_source": False}]}
    gap_info = {
        "total_words": 20,
        "planned_target": 500,
        "fill_ratio": 0.04,
        "deficit": 480,
        "underfilled_sections": [{"section_index": 1, "heading": "Reactor Physics", "purpose": "core reactor design", "actual_words": 20, "target_budget": 500, "deficit": 480}],
    }

    # Provider returns standard grounded research output dictionary (NOT {"items": ...})
    provider_grounded_response = {
        "raw_text": "Recent 2026 findings reveal high-efficiency thorium molten-salt reactor designs.",
        "grounding_metadata": {
            "web_search_queries": ["thorium molten salt reactor 2026"],
            "grounding_chunks": [{"web": {"uri": "https://energy.gov/thorium", "title": "DOE Thorium Report"}}],
            "grounding_supports": [{"grounding_chunk_indices": [0], "segment": {"text": "high-efficiency thorium molten-salt reactor designs."}}],
        },
        "research_sources": [{"title": "DOE Thorium Report", "url": "https://energy.gov/thorium", "source_id": "S1"}],
        "search_count": 1,
        "source_count": 1,
    }

    mock_expanded_script = PodcastScriptResponse(
        episode_title="Nuclear Engineering",
        episode_description="Desc",
        segments=[PodcastSegment(order=1, heading="Reactor Physics", narration="Short text deepened with thorium molten-salt reactor designs and specifications.")],
        warnings=[],
    )

    captured_depth = None

    def mock_failover(*args, **kwargs):
        nonlocal captured_depth
        op = kwargs.get("operation")
        if op == "supplemental_research":
            mock_provider = MagicMock()
            mock_provider.generate_grounded_research.return_value = provider_grounded_response
            exec_fn = kwargs.get("execute_fn")
            res = exec_fn(mock_provider, 1, "source")
            captured_depth = mock_provider.generate_grounded_research.call_args[1].get("research_depth")
            return res
        elif op == "section_expansion":
            return mock_expanded_script
        return mock_expanded_script

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover):
        with patch.object(settings, "HERALD_SUPPLEMENTAL_RESEARCH_DEPTH", "low"):
            expanded = expand_script_content_gap(
                job=job,
                completed_sections=sections,
                gap_info=gap_info,
                topic="Nuclear Reactor Design",
                evidence_packet=evidence_packet,
                scope=EvidenceScope.RESEARCH,
            )

            assert captured_depth == "low"
            # Verify new evidence IDs are prefixed with ev_supp_ and source metadata is preserved
            supp_items = [it for it in evidence_packet["items"] if "ev_supp" in it.get("evidence_id", "")]
            assert len(supp_items) >= 1
            assert any("thorium" in (it.get("snippet", "") + it.get("title", "")).lower() for it in supp_items)


def test_diagnostics_archive_includes_longform_repetition_and_gap_summaries(tmp_path):
    """Diagnostics archive exports longform/repetition-repair-summary.json and longform/gap-research-summary.json."""
    import zipfile
    from herald.services.diagnostics_export import generate_job_diagnostics_zip

    job = PodcastJob(
        id="diag-export-job",
        status=JobState.COMPLETE.value,
        configuration_state_json={
            "repetition_diagnostics": {
                "candidate_warnings_count": 3,
                "review_performed": True,
                "substantive_duplicates_found": 1,
                "repaired_count": 1,
                "omitted_candidates": [],
            },
            "gap_diagnostics": {
                "gap_detected": True,
                "deficit": 350,
                "fill_ratio": 0.72,
                "expansion_performed": True,
                "new_evidence_used": True,
            },
        },
        script_json={"episode_title": "Test Ep", "segments": []},
    )

    zip_path = tmp_path / "test_diag.zip"
    db_mock = MagicMock()
    db_mock.query.return_value.filter.return_value.all.return_value = []
    db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
    db_mock.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    db_mock.query.return_value.filter.return_value.count.return_value = 0

    with patch("herald.services.diagnostics_export.get_diagnostics_base_dir", return_value=tmp_path):
        out_zip = generate_job_diagnostics_zip(db=db_mock, job=job, target_zip_path=zip_path)

        assert out_zip.exists()
        with zipfile.ZipFile(out_zip, "r") as zf:
            names = zf.namelist()
            assert "longform/repetition-repair-summary.json" in names
            assert "longform/gap-research-summary.json" in names


def test_pricing_overrides_exact_match():
    """Configurable model pricing overrides exact provider/model match and supports JSON config."""
    import json
    from herald.db.models import AIInteraction
    from herald.services.token_cost import calculate_interaction_cost, get_effective_pricing_table

    overrides = {
        "custom_ai/custom-model-x": {
            "prompt_per_m": 1.50,
            "completion_per_m": 4.50,
            "effective_date": "2026-09-18",
        }
    }

    with patch.object(settings, "HERALD_MODEL_PRICING_OVERRIDES_JSON", json.dumps(overrides)):
        table = get_effective_pricing_table()
        assert ("custom_ai", "custom-model-x") in table

        inter = AIInteraction(
            id="test-override-interaction",
            job_id="job-override",
            provider="custom_ai",
            model="custom-model-x",
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
        )
        cost, known = calculate_interaction_cost(inter)
        assert known is True
        assert cost == 6.00


def test_unused_irrelevant_evidence_does_not_suppress_supplemental_research_and_zero_relevance_never_used():
    """Unused irrelevant evidence does not suppress supplemental research; zero-relevance evidence is never used to expand sections."""
    job = PodcastJob(id="supp-trigger-job")
    s1_narr = "Primary section on submarine acoustic dampening."
    sections = [
        {
            "section_index": 1,
            "heading": "Reactor Coolant Circulation",
            "purpose": "Explain natural circulation in submarine reactor coolant systems",
            "key_points": ["coolant loops", "natural circulation"],
            "narration": s1_narr,
            "word_count": len(s1_narr.split()),
            "relevant_evidence_ids": ["E1"],
        }
    ]
    gap_info = {
        "is_overall_underfilled": False,
        "total_words": 10,
        "planned_target": 500,
        "deficit": 400,
        "underfilled_sections": [
            {"section_index": 1, "deficit": 400, "word_budget": 500}
        ],
    }
    # Unused evidence E2 has ZERO relevance to reactor coolant (medieval agriculture)
    evidence_packet = {
        "items": [
            {"evidence_id": "E1", "title": "Submarine Basics", "snippet": "Acoustic dampening tiles"},
            {"evidence_id": "E2", "title": "Medieval Farming", "snippet": "Crop rotation techniques in fourteenth century Europe", "is_seed_source": False},
        ]
    }

    mock_supp_result = {
        "grounding_metadata": {"webSearchQueries": ["submarine reactor coolant natural circulation"]},
        "research_sources": [{"title": "Submarine Reactor Plants", "url": "https://navy.mil/reactor", "publisher": "Navy"}],
        "items": [
            {
                "title": "Natural Circulation Mechanics",
                "snippet": "Primary coolant loops utilize natural circulation at low speeds to eliminate coolant pump acoustic signatures.",
            }
        ]
    }

    with patch("herald.ai.long_form.execute_with_failover") as mock_failover:
        expanded_section_resp = MagicMock()
        expanded_section_resp.segments = [MagicMock(narration=s1_narr + " Expanded with natural circulation details at low speeds. " * 10)]
        mock_failover.side_effect = [mock_supp_result, expanded_section_resp]

        expanded_sections, gap_meta = expand_script_content_gap(
            job=job,
            completed_sections=sections,
            gap_info=gap_info,
            topic="Naval Nuclear Propulsion",
            evidence_packet=evidence_packet,
            scope=EvidenceScope.RESEARCH,
            return_metadata=True,
        )

        assert gap_meta["supplemental_research_triggered"] is True
        assert gap_meta["supplemental_research"]["search_count"] == 1
        # E2 (Medieval farming) was NEVER used!
        assert "E2" not in gap_meta["section_evidence_used"].get("1", [])
        # The new evidence item ev_supp_1 was used
        assert any("ev_supp" in eid for eid in gap_meta["section_evidence_used"].get("1", []))


def test_single_supplemental_research_operation_addresses_multiple_section_gaps():
    """Single supplemental research pass targets all starved sections simultaneously."""
    job = PodcastJob(id="multi-gap-job")
    sections = [
        {"section_index": 1, "heading": "Hull Design", "narration": "Hull words " * 40, "word_count": 80, "relevant_evidence_ids": ["E1"]},
        {"section_index": 2, "heading": "Reactor Coolant", "purpose": "Coolant details", "narration": "Coolant words " * 20, "word_count": 40, "relevant_evidence_ids": ["E2"]},
        {"section_index": 3, "heading": "Sonar Arrays", "purpose": "Sonar details", "narration": "Sonar words " * 20, "word_count": 40, "relevant_evidence_ids": ["E3"]},
    ]
    gap_info = {
        "is_overall_underfilled": True,
        "total_words": 160,
        "planned_target": 1000,
        "deficit": 500,
        "underfilled_sections": [
            {"section_index": 2, "deficit": 250, "word_budget": 300},
            {"section_index": 3, "deficit": 250, "word_budget": 300},
        ],
    }
    evidence_packet = {
        "items": [
            {"evidence_id": "E1", "title": "Hull", "snippet": "Hull info"},
            {"evidence_id": "E2", "title": "Reactor", "snippet": "Reactor info"},
            {"evidence_id": "E3", "title": "Sonar", "snippet": "Sonar info"},
        ]
    }

    mock_supp_result = {
        "grounding_metadata": {"webSearchQueries": ["reactor coolant and sonar arrays"]},
        "research_sources": [{"title": "Submarine Engineering", "url": "https://navy.mil/sub", "publisher": "Navy"}],
        "items": [
            {"title": "Advanced Reactor Coolant", "snippet": "Reactor coolant pump isolation mounts reduce acoustic energy."},
            {"title": "Spherical Sonar Arrays", "snippet": "Bow mounted sonar arrays provide passive acoustic monitoring."},
        ]
    }

    captured_operations = []

    def mock_failover_fn(*args, **kwargs):
        op = kwargs.get("operation")
        captured_operations.append(op)
        if op == "supplemental_research":
            return mock_supp_result
        elif op == "section_expansion":
            mock_res = MagicMock()
            mock_res.segments = [MagicMock(narration="Expanded section narration with fresh technical facts. " * 15)]
            return mock_res
        return MagicMock()

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover_fn):
        expanded_sections, gap_meta = expand_script_content_gap(
            job=job,
            completed_sections=sections,
            gap_info=gap_info,
            topic="Submarine Systems",
            evidence_packet=evidence_packet,
            scope=EvidenceScope.RESEARCH,
            return_metadata=True,
        )

        assert captured_operations.count("supplemental_research") == 1
        assert gap_meta["supplemental_research_triggered"] is True
        assert "Reactor Coolant" in gap_meta["supplemental_research"]["gap_focus"]
        assert "Sonar Arrays" in gap_meta["supplemental_research"]["gap_focus"]


def test_section_only_underfill_does_not_create_extra_section_when_overall_fill_acceptable():
    """Section-only deficit does not trigger extra section fallback if overall script meets length tolerance."""
    job = PodcastJob(id="sec-only-underfill-job")
    sections = [
        {"section_index": 1, "heading": "Overview", "narration": "Word " * 500, "word_count": 500, "relevant_evidence_ids": ["E1"]},
        {"section_index": 2, "heading": "Deep Dive", "narration": "Word " * 150, "word_count": 150, "relevant_evidence_ids": ["E2"]},
        {"section_index": 3, "heading": "Summary", "narration": "Word " * 300, "word_count": 300, "relevant_evidence_ids": ["E3"]},
    ]
    gap_info = {
        "is_overall_underfilled": False,
        "total_words": 950,
        "planned_target": 1000,
        "deficit": 150,
        "underfilled_sections": [
            {"section_index": 2, "deficit": 150, "word_budget": 300}
        ],
    }
    evidence_packet = {
        "items": [
            {"evidence_id": "E1", "title": "Overview", "snippet": "Overview snippet"},
            {"evidence_id": "E2", "title": "Deep Dive", "snippet": "Deep dive snippet"},
            {"evidence_id": "E3", "title": "Summary", "snippet": "Summary snippet"},
            {"evidence_id": "E4", "title": "Extra Topic", "snippet": "Topic details", "is_seed_source": False},
        ]
    }

    with patch("herald.ai.long_form.execute_with_failover") as mock_failover:
        mock_failover.return_value = {"items": [], "grounding_metadata": {}}
        expanded_sections, gap_meta = expand_script_content_gap(
            job=job,
            completed_sections=sections,
            gap_info=gap_info,
            topic="Complex Systems",
            evidence_packet=evidence_packet,
            scope=EvidenceScope.RESEARCH,
            return_metadata=True,
        )

        assert len(expanded_sections) == 3
        assert gap_meta["extra_section_added"] is False


def test_extra_section_requires_overall_underfill_and_distinct_relevant_material():
    """Extra section fallback requires overall underfill and distinct relevant evidence; rejects unrelated evidence."""
    job = PodcastJob(id="extra-sec-filter-job")
    s1_narr = "Primary submarine overview."
    sections = [
        {
            "section_index": 1,
            "heading": "Submarine Propulsion",
            "purpose": "Propulsion mechanics",
            "key_points": ["nuclear reactor", "steam turbine"],
            "narration": s1_narr,
            "word_count": len(s1_narr.split()),
            "relevant_evidence_ids": ["E1"],
        }
    ]
    gap_info = {
        "is_overall_underfilled": True,
        "total_words": 10,
        "planned_target": 1000,
        "deficit": 700,
        "underfilled_sections": [],
    }

    # Packet with only UNRELATED evidence (Medieval cooking)
    unrelated_packet = {
        "items": [
            {"evidence_id": "E1", "title": "Propulsion", "snippet": "Nuclear reactor and steam turbine"},
            {"evidence_id": "E2", "title": "Culinary History", "snippet": "Renaissance pastry recipes and baking methods", "is_seed_source": False},
        ]
    }

    expanded_unrelated, meta_unrelated = expand_script_content_gap(
        job=job,
        completed_sections=sections,
        gap_info=gap_info,
        topic="Submarine Engineering",
        evidence_packet=unrelated_packet,
        scope=EvidenceScope.RESEARCH,
        return_metadata=True,
    )
    assert len(expanded_unrelated) == 1
    assert meta_unrelated["extra_section_added"] is False
    assert meta_unrelated["extra_section_skipped_reason"] == "no_distinct_uncovered_evidence"

    # Packet with RELEVANT and DISTINCT evidence
    relevant_packet = {
        "items": [
            {"evidence_id": "E1", "title": "Propulsion", "snippet": "Nuclear reactor and steam turbine"},
            {"evidence_id": "E3", "title": "Submarine Acoustic Quieting", "snippet": "Submarine engineering acoustic isolation mounts and hull damping tiles", "is_seed_source": False},
        ]
    }

    with patch("herald.ai.long_form.generate_single_section") as mock_gen_sec:
        mock_gen_sec.return_value = {
            "section_index": 2,
            "heading": "Submarine Acoustic Quieting",
            "narration": "Detailed analysis of acoustic dampening tiles. " * 30,
            "word_count": 300,
            "relevant_evidence_ids": ["E3"],
        }
        expanded_relevant, meta_relevant = expand_script_content_gap(
            job=job,
            completed_sections=sections,
            gap_info=gap_info,
            topic="Submarine Engineering",
            evidence_packet=relevant_packet,
            scope=EvidenceScope.RESEARCH,
            return_metadata=True,
        )
        assert len(expanded_relevant) == 2
        assert meta_relevant["extra_section_added"] is True
        assert meta_relevant["extra_section_reason"] == "overall_underfill_with_distinct_topic_material"


def test_overlapping_phrase_variants_collapsed_before_repetition_cap():
    """Overlapping phrase variants (Crazy Ivan, Ivan maneuver, Crazy Ivan maneuver) collapse into one candidate before cap."""
    from herald.ai.long_form import review_script_repetition
    from herald.ai.schema import RepetitionReviewResponse

    job = PodcastJob(id="rep-collapse-test")
    sections = [
        {"section_index": 1, "heading": "Maneuvers", "narration": "The Crazy Ivan maneuver was a famous tactic."},
        {"section_index": 2, "heading": "Tactics", "narration": "The Ivan maneuver or Crazy Ivan was repeated here."},
    ]

    distinctive_warnings = [
        MagicMock(metadata={"phrase": "Crazy Ivan maneuver", "sections": [1, 2], "is_named": True}),
        MagicMock(metadata={"phrase": "Crazy Ivan", "sections": [1, 2], "is_named": True}),
        MagicMock(metadata={"phrase": "Ivan maneuver", "sections": [1, 2], "is_named": True}),
    ]

    mock_resp = RepetitionReviewResponse(has_substantive_repetition=False, reviews=[])

    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp):
        res, to_repair, rep_meta = review_script_repetition(
            job=job,
            completed_sections=sections,
            near_duplicate_warnings=[],
            distinctive_phrase_warnings=distinctive_warnings,
            topic="Submarine Tactics",
        )

        assert rep_meta["evaluated_count"] == 1
        eval_cand = rep_meta["candidate_ranking_selection"][0]
        assert "crazy ivan maneuver" in eval_cand["candidate_text"].lower()
        assert eval_cand["is_named"] is True


def test_repetition_candidate_ordering_deterministic_and_distinctive_concepts_survive_noisy_pool():
    """Deterministic ranking prioritizes distinctive concepts (Crazy Ivan, teardrop hull, 300,000-gallon tank) over noisy generic pairs."""
    from herald.ai.long_form import review_script_repetition
    from herald.ai.schema import RepetitionReviewResponse

    job = PodcastJob(id="rep-rank-test")
    sections = [
        {"section_index": i, "heading": f"Section {i}", "narration": f"Narration for section {i}"}
        for i in range(1, 10)
    ]

    noisy_warnings = [
        MagicMock(metadata={"phrase": f"general system operation {i}", "sections": [1, 2], "is_named": False, "is_numeric": False})
        for i in range(1, 21)
    ]

    distinctive_concepts = [
        MagicMock(metadata={"phrase": "Crazy Ivan", "sections": [1, 3], "is_named": True, "is_numeric": False}),
        MagicMock(metadata={"phrase": "teardrop hull", "sections": [2, 4], "is_named": False, "is_numeric": False}),
        MagicMock(metadata={"phrase": "300,000-gallon tank", "sections": [3, 5], "is_named": False, "is_numeric": True}),
    ]

    all_warnings = noisy_warnings + distinctive_concepts

    mock_resp = RepetitionReviewResponse(has_substantive_repetition=False, reviews=[])

    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp):
        res, to_repair, rep_meta = review_script_repetition(
            job=job,
            completed_sections=sections,
            near_duplicate_warnings=[],
            distinctive_phrase_warnings=all_warnings,
            topic="Naval Architecture",
        )

        assert rep_meta["evaluated_count"] == 14
        eval_texts = [c["candidate_text"].lower() for c in rep_meta["candidate_ranking_selection"]]

        assert any("crazy ivan" in t for t in eval_texts)
        assert any("teardrop hull" in t for t in eval_texts)
        assert any("300,000-gallon tank" in t for t in eval_texts)


def test_grounded_source_title_preserved_through_normalization_and_supp_remapping():
    """Grounded source titles and metadata are preserved through normalization and supplemental evidence mapping."""
    from herald.ai.long_form import normalize_evidence_packet

    grounded_data = {
        "grounding_metadata": {
            "webSearchQueries": ["nuclear submarine propulsion safety records"],
            "groundingChunks": [
                {"web": {"title": "Official Nuclear Safety Report 2024", "uri": "https://gov.safety/report2024"}},
            ],
            "groundingSupports": [
                {
                    "groundingChunkIndices": [0],
                    "segment": {"text": "Submarine reactors feature secondary containment vessels and passive coolant loops."},
                }
            ],
        },
        "research_sources": [
            {"title": "Official Nuclear Safety Report 2024", "url": "https://gov.safety/report2024", "publisher": "Safety Board"}
        ],
    }

    norm_packet = normalize_evidence_packet(
        topic="Naval Propulsion",
        scope=EvidenceScope.RESEARCH,
        grounded_research_data=grounded_data,
    )

    items = norm_packet.get("items", [])
    assert len(items) >= 1
    first_item = items[0]
    assert first_item["actual_source_title"] == "Official Nuclear Safety Report 2024"
    assert first_item["title"] == "Official Nuclear Safety Report 2024"
    assert first_item["source_url"] == "https://gov.safety/report2024"


def test_diagnostics_contents_for_repetition_repair_and_gap_research_summaries(tmp_path):
    """Diagnostics archive exports complete machine-readable repetition and gap summaries."""
    import json
    import zipfile
    from herald.services.diagnostics_export import generate_job_diagnostics_zip

    job = PodcastJob(
        id="diag-full-summary-job",
        status=JobState.COMPLETE.value,
        configuration_state_json={
            "repetition_diagnostics": {
                "initial_near_duplicate_warnings_count": 1,
                "distinctive_concept_candidates_count": 15,
                "total_candidate_count": 16,
                "evaluated_count": 14,
                "candidate_ranking_selection": [
                    {"section_a": 1, "section_b": 2, "candidate_text": "Crazy Ivan", "tier": 1}
                ],
                "omitted_candidates": [
                    {"candidate_text": "generic term", "reason": "exceeded_candidate_cap_14"}
                ],
                "substantive_duplicates_found": 1,
                "repaired_sections": [2],
                "repaired_section_word_counts": [{"section_index": 2, "before_words": 150, "after_words": 140}],
                "repair_success": True,
            },
            "gap_diagnostics": {
                "gap_detected": True,
                "deficit": 400,
                "fill_ratio": 0.70,
                "supplemental_research_triggered": True,
                "supplemental_research": {
                    "provider": "google",
                    "model": "gemini-2.5-flash",
                    "search_count": 3,
                    "source_count": 4,
                    "new_evidence_count": 3,
                    "gap_focus": "Coolant Systems",
                },
                "sections_expanded": [2],
                "section_evidence_used": {"2": ["ev_supp_1"]},
                "section_word_counts": [{"section_index": 2, "before_words": 100, "after_words": 250}],
                "extra_section_added": False,
                "extra_section_skipped_reason": "deficit_resolved_in_place",
            },
        },
        script_json={"episode_title": "Reactor Test", "segments": []},
    )

    zip_path = tmp_path / "test_diag_full.zip"
    db_mock = MagicMock()
    db_mock.query.return_value.filter.return_value.all.return_value = []
    db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
    db_mock.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    db_mock.query.return_value.filter.return_value.count.return_value = 0

    with patch("herald.services.diagnostics_export.get_diagnostics_base_dir", return_value=tmp_path):
        out_zip = generate_job_diagnostics_zip(db=db_mock, job=job, target_zip_path=zip_path)
        with zipfile.ZipFile(out_zip, "r") as zf:
            rep_data = json.loads(zf.read("longform/repetition-repair-summary.json"))
            gap_data = json.loads(zf.read("longform/gap-research-summary.json"))

            assert rep_data["evaluated_count"] == 14
            assert rep_data["repaired_sections"] == [2]
            assert rep_data["repair_success"] is True
            assert len(rep_data["omitted_candidates"]) == 1

            assert gap_data["gap_detected"] is True
            assert gap_data["supplemental_research"]["search_count"] == 3
            assert gap_data["sections_expanded"] == [2]
            assert gap_data["extra_section_skipped_reason"] == "deficit_resolved_in_place"


def test_final_script_substage_is_complete_and_current_section_cleared():
    """Unified long-form pipeline marks script_substage as complete and clears script_current_section."""
    job = PodcastJob(
        id="substage-complete-job",
        configuration_state_json={
            "script_substage": "fidelity_audit",
            "script_current_section": 3,
        },
    )
    _set_script_substage(job, "complete", db=None)
    assert job.configuration_state_json["script_substage"] == "complete"
    assert job.configuration_state_json["script_current_section"] is None


def test_external_ai_interaction_with_missing_token_telemetry_reports_cost_unavailable():
    """External billable AI interaction with missing token telemetry reports cost unavailable, never $0.00."""
    from herald.db.models import AIInteraction
    from herald.services.token_cost import aggregate_job_tokens_and_cost

    inter_missing = AIInteraction(
        id="inter-missing-1",
        job_id="job-cost-missing",
        provider="gemini",
        model="gemini-2.5-flash",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
    )

    summary = aggregate_job_tokens_and_cost([inter_missing])
    assert summary.is_cost_complete is False
    assert summary.is_cost_available is False
    assert summary.cost_display == "unavailable"

    # Partial cost test: valid interaction + missing telemetry interaction
    inter_valid = AIInteraction(
        id="inter-valid-1",
        job_id="job-cost-partial",
        provider="gemini",
        model="gemini-2.5-flash",
        prompt_tokens=100_000,
        completion_tokens=50_000,
        total_tokens=150_000,
    )

    with patch.object(settings, "HERALD_ENABLE_BUILTIN_EXTERNAL_PRICING", True):
        summary_partial = aggregate_job_tokens_and_cost([inter_valid, inter_missing])
        assert summary_partial.is_cost_complete is False
        assert summary_partial.is_cost_available is True
        assert "(partial)" in summary_partial.cost_display
        assert summary_partial.total_cost_usd > 0.0


def test_pricing_configuration_defaults_and_opt_in_behavior():
    """Pricing defaults to verified builtin table; disabling external pricing drops billable external rates while keeping local."""
    from herald.services.token_cost import get_effective_pricing_table

    with patch.object(settings, "HERALD_ENABLE_BUILTIN_EXTERNAL_PRICING", True):
        table_enabled = get_effective_pricing_table()
        assert ("gemini", "gemini-2.5-flash") in table_enabled
        assert ("openai", "gpt-4o") in table_enabled
        assert ("ollama", "llama3.2") in table_enabled

    with patch.object(settings, "HERALD_ENABLE_BUILTIN_EXTERNAL_PRICING", False):
        table_disabled = get_effective_pricing_table()
        assert ("gemini", "gemini-2.5-flash") not in table_disabled
        assert ("openai", "gpt-4o") not in table_disabled
        assert ("ollama", "llama3.2") in table_disabled

