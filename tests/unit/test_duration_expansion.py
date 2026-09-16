"""
Unit tests for duration and section expansion budgeting.
"""

from unittest.mock import patch

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
