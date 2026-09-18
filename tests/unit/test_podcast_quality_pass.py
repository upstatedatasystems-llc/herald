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
    }

    evidence_packet = {
        "items": [
            {"evidence_id": "E1", "title": "Primary Doc", "snippet": "Core architecture info"},
            {"evidence_id": "E2", "title": "Secondary Doc", "snippet": "Uncovered performance data", "is_seed_source": False},
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
        res, to_repair = review_script_repetition(
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
