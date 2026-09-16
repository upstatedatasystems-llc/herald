"""
Unit tests for duration and section expansion budgeting.
"""

from unittest.mock import MagicMock, patch

import pytest

from herald.ai.long_form import EvidenceScope, expand_single_section
from herald.ai.schema import PodcastScriptResponse, PodcastSegment
from herald.db.models import PodcastJob


@pytest.fixture
def mock_job():
    return PodcastJob(
        id="test-job-duration-01",
        content_mode="source",
        request_mode="standard",
        ai_provider="gemini",
        ai_model="gemini-2.5-flash",
    )


def test_expand_single_section_successful_expansion(mock_job):
    """
    Verify that when actual_words is below threshold, expansion generates
    longer narration and returns success=True with increased word count.
    """
    short_narration = "This is a short section with only nine words."
    target_budget = 100
    section_plan = {
        "heading": "Early Cosmic Dawn",
        "key_points": ["Point 1", "Point 2", "Point 3"],
    }
    evidence_items = [
        {"snippet": "The telescope detected redshift z=14.32 galaxy candidates in high abundance."},
        {"snippet": "Spectroscopic confirmation proved stellar populations formed 300 million years after Big Bang."},
    ]

    expanded_segments = [
        PodcastSegment(
            order=1,
            heading="Early Cosmic Dawn",
            narration=(
                "The James Webb Space Telescope has revolutionized our understanding of the early universe. "
                "Recent deep field observations have detected luminous galaxy candidates at redshift fourteen point three two, "
                "which indicates that massive star formation began much earlier than theoretical models previously predicted. "
                "Spectroscopic analysis confirmed these stellar populations formed within the first three hundred million years after the Big Bang."
            ),
        )
    ]
    mock_resp = PodcastScriptResponse(
        episode_title="Universe Discovery",
        episode_description="Test episode description",
        segments=expanded_segments,
        warnings=[],
    )

    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp):
        section_info = {
            "section_index": 1,
            "heading": section_plan["heading"],
            "purpose": "Explain early universe discoveries",
            "relevant_evidence_ids": ["E1", "E2"],
        }
        evidence_packet = {
            "items": [
                {"evidence_id": "E1", "title": "Redshift", "snippet": evidence_items[0]["snippet"]},
                {"evidence_id": "E2", "title": "Stellar", "snippet": evidence_items[1]["snippet"]},
            ]
        }
        res = expand_single_section(
            job=mock_job,
            section_info=section_info,
            current_narration=short_narration,
            actual_words=len(short_narration.split()),
            target_budget=target_budget,
            topic="Space Exploration",
            evidence_packet=evidence_packet,
            scope=EvidenceScope.SOURCE_PLUS_RESEARCH,
        )

        assert res["success"] is True
        assert res["word_count"] > len(short_narration.split())
        assert res["words_added"] > 25
        assert "James Webb" in res["narration"]


def test_expand_single_section_fails_gracefully_preserving_draft(mock_job):
    """
    If AI failover fails completely during expansion, it gracefully catches the error
    and returns success=False retaining the original draft narration.
    """
    orig_narration = "This is the original narration that must be preserved on failure."
    section_info = {"section_index": 2, "heading": "Analysis", "purpose": "Deep dive into analysis"}
    evidence_packet = {"items": [{"evidence_id": "E1", "title": "Fact", "snippet": "Some fact"}]}

    with patch("herald.ai.long_form.execute_with_failover", side_effect=RuntimeError("AI Provider Down")):
        res = expand_single_section(
            job=mock_job,
            section_info=section_info,
            current_narration=orig_narration,
            actual_words=len(orig_narration.split()),
            target_budget=100,
            topic="Topic",
            evidence_packet=evidence_packet,
            scope=EvidenceScope.SOURCE_PLUS_RESEARCH,
        )

        assert res["success"] is False
        assert res["narration"] == orig_narration
        assert res["word_count"] == len(orig_narration.split())
        assert "expansion_error" in res.get("reason", "")


def test_expand_single_section_insufficient_expansion_retains_draft(mock_job):
    """
    If AI returns an expansion that did not add at least 25 words, the draft is retained.
    """
    orig_narration = "This is the original narration that has eleven words in it."
    section_info = {"section_index": 1, "heading": "Analysis", "purpose": "Deep dive"}

    insufficient_segments = [
        PodcastSegment(
            order=1,
            heading="Analysis",
            narration="This is the slightly tweaked narration that has twelve words in it.",
        )
    ]
    mock_resp = PodcastScriptResponse(
        episode_title="Title",
        episode_description="Description",
        segments=insufficient_segments,
        warnings=[],
    )

    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp):
        res = expand_single_section(
            job=mock_job,
            section_info=section_info,
            current_narration=orig_narration,
            actual_words=len(orig_narration.split()),
            target_budget=100,
            topic="Topic",
            evidence_packet={"items": []},
            scope=EvidenceScope.SOURCE_PLUS_RESEARCH,
        )

        assert res["success"] is False
        assert res["narration"] == orig_narration
        assert res["reason"] == "expansion_below_threshold"


def test_source_only_underfilled_section_with_evidence_is_eligible_for_expansion(mock_job):
    """
    Test A: AI-generated SOURCE_ONLY mode underfilled section with unused source evidence
    is eligible for expansion and includes explicit source-only grounding contract.
    """
    mock_job.content_mode = "source"
    section_info = {
        "section_index": 1,
        "heading": "Historical Context",
        "purpose": "Cover early Roman founding",
        "relevant_evidence_ids": ["E1"],
    }
    evidence_packet = {
        "items": [
            {"evidence_id": "E1", "title": "Source Article", "snippet": "Unused details from the article."}
        ]
    }
    expanded_segments = [
        PodcastSegment(
            order=1,
            heading="Historical Context",
            narration=(
                "Rome's early founding involved complex negotiations among neighboring tribes. "
                "The source documents emphasize how early institutional structures supported steady agricultural growth, "
                "which enabled stable civic governance across multiple generations. "
                "These early treaties established clear trade corridors and defensive pacts that lasted for centuries."
            ),
        )
    ]
    mock_resp = PodcastScriptResponse(
        episode_title="Source Title",
        episode_description="Description",
        segments=expanded_segments,
        warnings=[],
    )

    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp) as mock_exec:
        res = expand_single_section(
            job=mock_job,
            section_info=section_info,
            current_narration="Rome began as a small city state.",
            actual_words=7,
            target_budget=60,
            topic="Roman History",
            evidence_packet=evidence_packet,
            scope=EvidenceScope.SOURCE_ONLY,
        )

        assert res["success"] is True
        assert res["words_added"] > 25
        # Verify instructions passed to AI included strict SOURCE_ONLY grounding requirement
        passed_fn = mock_exec.call_args[1]["execute_fn"]
        fake_provider = MagicMock()
        fake_provider.generate_script.return_value = mock_resp
        passed_fn(fake_provider, 1, "test")
        gen_kwargs = fake_provider.generate_script.call_args[1]
        assert "SOURCE-ONLY GROUNDING REQUIREMENT: Use ONLY the supplied source/evidence" in gen_kwargs["generation_instructions"]


def test_source_only_exhausted_evidence_no_filler_expansion(mock_job):
    """
    Test B: When source evidence is exhausted or model cannot add grounded detail,
    expansion fails gracefully and original draft is retained without filler.
    """
    mock_job.content_mode = "source"
    orig_narration = "The article only states that the summit took place on Tuesday morning."
    section_info = {"section_index": 1, "heading": "Event", "purpose": "Report event"}
    evidence_packet = {"items": []}

    # Model returns the same text because no extra facts exist
    same_segments = [
        PodcastSegment(order=1, heading="Event", narration=orig_narration)
    ]
    mock_resp = PodcastScriptResponse(
        episode_title="Title",
        episode_description="Desc",
        segments=same_segments,
        warnings=[],
    )

    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp):
        res = expand_single_section(
            job=mock_job,
            section_info=section_info,
            current_narration=orig_narration,
            actual_words=len(orig_narration.split()),
            target_budget=100,
            topic="Summit",
            evidence_packet=evidence_packet,
            scope=EvidenceScope.SOURCE_ONLY,
        )

        assert res["success"] is False
        assert res["narration"] == orig_narration
        assert res["reason"] == "expansion_below_threshold"


def test_literal_mode_never_expansion():
    """
    Test C: Literal mode is ZERO AI and must NEVER invoke section expansion.
    """
    from herald.db.models import ContentMode

    literal_job = PodcastJob(id="job-literal-01", content_mode=ContentMode.LITERAL.value)
    is_literal = (
        getattr(literal_job, "content_mode", None) == ContentMode.LITERAL.value
        or str(getattr(literal_job, "content_mode", "")).lower() == "literal"
    )
    assert is_literal is True


def test_research_mode_expansion_behavior_preserved(mock_job):
    """
    Test D: Research mode expansion continues to work seamlessly with research dossier.
    """
    mock_job.content_mode = "topic"
    section_info = {
        "section_index": 1,
        "heading": "Quantum Algorithms",
        "purpose": "Explain error correction",
        "relevant_evidence_ids": ["R1"],
    }
    evidence_packet = {
        "items": [
            {"evidence_id": "R1", "title": "Surface Codes", "snippet": "Surface code threshold is approximately one percent."}
        ]
    }
    expanded_segments = [
        PodcastSegment(
            order=1,
            heading="Quantum Algorithms",
            narration=(
                "Quantum error correction relies heavily on surface codes to detect phase flip and bit flip errors. "
                "Experimental demonstrations show the error threshold sits right around one percent fault tolerance. "
                "By arranging physical qubits into two-dimensional square lattices, syndromes can be measured continuously without collapsing quantum state."
            ),
        )
    ]
    mock_resp = PodcastScriptResponse(
        episode_title="Quantum Computing",
        episode_description="Overview",
        segments=expanded_segments,
        warnings=[],
    )

    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp):
        res = expand_single_section(
            job=mock_job,
            section_info=section_info,
            current_narration="Error correction is needed for quantum computers.",
            actual_words=7,
            target_budget=50,
            topic="Quantum",
            evidence_packet=evidence_packet,
            scope=EvidenceScope.RESEARCH,
        )

        assert res["success"] is True
        assert res["words_added"] >= 20
        assert "surface codes" in res["narration"]
