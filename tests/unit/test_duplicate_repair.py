"""
Unit tests for CoverageLedger and bounded pre-TTS duplicate repair.
"""

from unittest.mock import patch

from herald.ai.long_form import CoverageLedger, repair_script_duplicates
from herald.ai.schema import PodcastScriptResponse, PodcastSegment
from herald.db.models import PodcastJob
from herald.services.quality_gate import QualitySeverity, QualityWarning


def test_coverage_ledger_compaction_and_size_guard():
    """
    Verify CoverageLedger records concepts, facts, key points and formats
    a compact context strictly within the 1,200 character budget.
    """
    ledger = CoverageLedger()

    for i in range(1, 6):
        sec_points = [f"Detailed scientific finding {i}.{j} explaining redshift candidate morphology" for j in range(1, 6)]
        ledger.record_section({
            "section_index": i,
            "heading": f"Section Heading {i}",
            "key_points": sec_points,
            "narration": f"Here is long narration for section {i} covering cosmic evolution and astrophysics.",
        })

    context_str = ledger.format_context(
        current_heading="Cosmic Microwave Background",
        current_purpose="Explain observations",
        current_idx=6,
    )
    assert len(context_str) <= 1200
    assert "Section 6" in context_str
    assert "COVERAGE LEDGER" in context_str


def test_repair_script_duplicates_replaces_redundancy():
    """
    Verify that repair_script_duplicates identifies offending duplicate sections
    and calls execute_with_failover to replace the redundant passages.
    """
    job = PodcastJob(id="test-dup-job-01", request_mode="standard")

    sections = [
        {
            "order": 1,
            "heading": "Cosmic Dawn",
            "narration": "The James Webb Space Telescope observed galaxy candidate JADES-GS-z14-0.",
            "word_count": 10,
        },
        {
            "order": 2,
            "heading": "Deep Field Spectrograms",
            "narration": "The James Webb Space Telescope observed galaxy candidate JADES-GS-z14-0. Also we measured light.",
            "word_count": 15,
        },
    ]

    warnings = [
        QualityWarning(
            code="NEAR_DUPLICATE_CROSS_SECTION",
            message="Section 2 repeats content from Section 1",
            section_index=2,
            severity=QualitySeverity.WARNING,
            metadata={
                "section_a": 1,
                "section_b": 2,
                "passage_a": "The James Webb Space Telescope observed galaxy candidate JADES-GS-z14-0.",
                "passage_b": "The James Webb Space Telescope observed galaxy candidate JADES-GS-z14-0.",
                "similarity": 0.85,
            },
        )
    ]

    repaired_segments = [
        PodcastSegment(
            order=1,
            heading="Deep Field Spectrograms",
            narration=(
                "Turning specifically to the spectroscopic data, the NIRSpec instrument revealed a distinct "
                "Lyman break that definitively places this galaxy in the early cosmic dawn. "
                "Furthermore, the observed ultraviolet continuum slope indicates a remarkably metal-poor stellar environment."
            ),
        )
    ]
    mock_resp = PodcastScriptResponse(
        episode_title="Title",
        episode_description="Episode description for test",
        segments=repaired_segments,
        warnings=[],
    )

    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp):
        repaired_sections, meta = repair_script_duplicates(
            job=job,
            sections=sections,
            duplicate_warnings=warnings,
            evidence_packet={"items": [{"snippet": "NIRSpec measured metal-poor environment."}]},
            topic="Cosmic Dawn",
        )

        assert meta["repair_attempted"] is True
        assert meta["repaired_count"] == 1
        assert "Lyman break" in repaired_sections[1]["narration"]
        assert "JADES-GS-z14-0" not in repaired_sections[1]["narration"]


def test_repair_script_duplicates_no_warnings_returns_cleanly():
    """When duplicate warnings list is empty, returns original sections without calling AI."""
    job = PodcastJob(id="test-clean-job")
    sections = [{"order": 1, "heading": "Heading", "narration": "Narration", "word_count": 1}]

    repaired, meta = repair_script_duplicates(
        job=job,
        sections=sections,
        duplicate_warnings=[],
        evidence_packet={},
        topic="Topic",
    )

    assert meta["repair_attempted"] is False
    assert meta["repaired_count"] == 0
    assert repaired == sections
