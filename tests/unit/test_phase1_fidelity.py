"""Unit tests for Phase 1 Fidelity Optimization, Content Warning, and Diagnostics."""

from unittest.mock import MagicMock, patch

import pytest

from herald.ai.long_form import (
    EvidenceScope,
    audit_and_repair_fidelity,
)
from herald.ai.schema import PodcastScriptResponse, PodcastSegment
from herald.db.models import AIInteraction, PodcastJob
from herald.services.diagnostics_export import build_manifest_dict
from herald.telegram.formatters import format_approval


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.count.return_value = 0
    return db


def test_audit_scopes_evidence_to_assigned_ids(mock_db):
    """Fidelity audit must scope evidence packet items to assigned chunk/evidence IDs."""
    job = PodcastJob(id="fid-job-1", custom_title="Fidelity Test")
    sections = [
        {
            "section_index": 1,
            "heading": "Intro",
            "narration": "Section 1 content.",
            "relevant_evidence_ids": ["ev_1"],
        },
        {
            "section_index": 2,
            "heading": "Details",
            "narration": "Section 2 content.",
            "relevant_evidence_ids": ["ev_3"],
        },
    ]
    evidence_packet = {
        "items": [
            {"evidence_id": "ev_1", "snippet": "Evidence 1 text"},
            {"evidence_id": "ev_2", "snippet": "Evidence 2 unused"},
            {"evidence_id": "ev_3", "snippet": "Evidence 3 text"},
            {"evidence_id": "ev_4", "snippet": "Evidence 4 unused"},
        ]
    }

    captured_dossier = None

    def fake_audit_research_script(source_text, research_dossier, script_dict, job_id):
        nonlocal captured_dossier
        captured_dossier = research_dossier
        mock_res = MagicMock()
        mock_res.has_material_issues = False
        mock_res.model_dump.return_value = {"has_material_issues": False}
        return mock_res

    fake_provider = MagicMock()
    fake_provider.audit_research_script.side_effect = fake_audit_research_script

    with patch("herald.ai.long_form.execute_with_failover", side_effect=lambda **kw: kw["execute_fn"](fake_provider, 1, kw.get("source_text"))):
        repaired, audit_res = audit_and_repair_fidelity(
            job=job,
            sections=sections,
            source_ledger=None,
            evidence_packet=evidence_packet,
            scope=EvidenceScope.RESEARCH,
            db=mock_db,
        )

    assert audit_res["status"] == "clean"
    assert audit_res["has_material_issues"] is False
    assert captured_dossier is not None
    # Verify only ev_1 and ev_3 are in the scoped dossier items
    scoped_ids = [it["evidence_id"] for it in captured_dossier.get("items", [])]
    assert scoped_ids == ["ev_1", "ev_3"]
    assert "ev_2" not in scoped_ids
    assert "ev_4" not in scoped_ids


def test_targeted_repair_and_content_warning_on_unresolved_issue(mock_db):
    """When repair does not resolve issues or fails, content_warning is set to True."""
    job = PodcastJob(id="fid-job-2", custom_title="Repair Warning Test")
    sections = [
        {
            "section_index": 1,
            "heading": "Intro Section",
            "narration": "Intro text without the key number 42 percent.",
            "relevant_evidence_ids": ["ev_1"],
        }
    ]
    evidence_packet = {"items": [{"evidence_id": "ev_1", "snippet": "Key statistic is 42 percent"}]}

    # Provider audit flags material issue
    mock_audit = MagicMock()
    mock_audit.has_material_issues = True
    mock_audit.repair_instructions = "Section 1 omitted the 42 percent statistic"
    mock_audit.model_dump.return_value = {"has_material_issues": True, "repair_instructions": mock_audit.repair_instructions}

    fake_provider = MagicMock()
    fake_provider.audit_research_script.return_value = mock_audit

    # Repair fails or re-audit still flags issue
    def fake_execute(job, operation, execute_fn, **kwargs):
        if operation in ("research_audit", "verification"):
            return execute_fn(fake_provider, 1, kwargs.get("source_text"))
        elif operation in ("research_repair", "verification_repair"):
            # Return repair that re-audit will still flag as broken
            return PodcastScriptResponse(
                episode_title="Test",
                episode_description="Desc",
                segments=[PodcastSegment(order=1, heading="Intro Section", narration="Still missing the key number")],
            )
        return None

    with patch("herald.ai.long_form.execute_with_failover", side_effect=fake_execute):
        repaired, audit_res = audit_and_repair_fidelity(
            job=job,
            sections=sections,
            source_ledger=None,
            evidence_packet=evidence_packet,
            scope=EvidenceScope.RESEARCH,
            db=mock_db,
        )

    # Re-audit found issues again
    assert audit_res["status"] == "unresolved_issue_remains"
    assert audit_res["unresolved_issue"] is True
    assert audit_res["content_warning"] is True


def test_format_approval_renders_content_warning_card():
    """format_approval renders warning header, explanation, and 'Synthesize Anyway' button."""
    job = PodcastJob(
        id="warn-job-12345678",
        custom_title="Controversial Episode",
        request_mode="topic",
        fidelity_audit_json={
            "content_warning": True,
            "unresolved_issue": True,
            "status": "unresolved_issue_remains",
            "repair_instructions": "Omission of conflicting economic data could not be verified automatically.",
        },
    )
    script_obj = {
        "episode_title": "Controversial Episode",
        "episode_description": "Economic analysis",
        "segments": [{"order": 1, "heading": "Analysis", "narration": "Some narration text."}],
    }

    text, markup = format_approval(job, script_obj)

    # Assert Content Warning in text
    assert "⚠️ <b>Podcast Ready for Approval (Content Warning)</b>" in text
    assert "⚠️ <b>CONTENT WARNING: Unresolved Factual Concern</b>" in text
    assert "Omission of conflicting economic data" in text

    # Assert Buttons
    keyboard = markup["inline_keyboard"][0]
    approve_btn = keyboard[0]
    deny_btn = keyboard[1]
    assert approve_btn["text"] == "⚠️ Synthesize Anyway"
    assert approve_btn["callback_data"] == f"h2:approve:{job.id}"
    assert deny_btn["text"] == "❌ Cancel"
    assert deny_btn["callback_data"] == f"h2:deny:{job.id}"


def test_format_approval_normal_without_content_warning():
    """When clean, format_approval renders standard card."""
    job = PodcastJob(
        id="clean-job-12345678",
        custom_title="Clean Episode",
        request_mode="source",
        fidelity_audit_json={"status": "clean", "content_warning": False, "has_material_issues": False},
    )
    script_obj = {
        "episode_title": "Clean Episode",
        "segments": [{"order": 1, "heading": "Intro", "narration": "Clean narration."}],
    }

    text, markup = format_approval(job, script_obj)

    assert "📋 <b>Podcast Ready for Approval</b>" in text
    assert "CONTENT WARNING" not in text
    keyboard = markup["inline_keyboard"][0]
    assert keyboard[0]["text"] == "✅ Approve & Generate"


def test_diagnostics_manifest_token_breakdown_and_section_progress(mock_db):
    """Manifest must handle list section_progress_json, token breakdown, and quality_gate."""
    job = PodcastJob(
        id="diag-job-123",
        request_mode="research",
        program_duration_seconds=300,
        section_progress_json=[
            {"section_index": 1, "heading": "S1", "word_count": 250},
            {"section_index": 2, "heading": "S2", "word_count": 350},
        ],
        configuration_state_json={
            "requested_target_words": 600,
            "evidence_supported_target_words": 600,
            "quality_gate": {"has_warnings": False, "warnings": []},
        },
        script_json={
            "segments": [
                {"order": 1, "heading": "S1", "narration": "word " * 250},
                {"order": 2, "heading": "S2", "narration": "word " * 350},
            ]
        },
    )

    # Mock AIInteraction rows
    mock_interactions = [
        AIInteraction(
            id="ai-1",
            job_id="diag-job-123",
            operation="grounded_research",
            total_tokens=1500,
            prompt_tokens=1000,
            completion_tokens=500,
        ),
        AIInteraction(
            id="ai-2",
            job_id="diag-job-123",
            operation="section_generation",
            total_tokens=3000,
            prompt_tokens=2000,
            completion_tokens=1000,
        ),
        AIInteraction(
            id="ai-3",
            job_id="diag-job-123",
            operation="research_audit",
            total_tokens=800,
            prompt_tokens=700,
            completion_tokens=100,
        ),
        AIInteraction(
            id="ai-4",
            job_id="diag-job-123",
            operation="research_repair",
            total_tokens=1200,
            prompt_tokens=800,
            completion_tokens=400,
        ),
    ]

    mock_db.query.return_value.filter.return_value.all.return_value = mock_interactions

    manifest = build_manifest_dict(
        job=job,
        db=mock_db,
        included_files=["manifest.json", "script.json"],
        truncated_files=[],
    )

    # 1. Section words populated from list
    assert manifest["section_words"] == [250, 350]
    assert manifest["effective_evidence_target_words"] == 600
    assert manifest["planned_words"] == 600

    # 2. Quality gate
    assert manifest["quality_gate"] == {"has_warnings": False, "warnings": []}

    # 3. Token breakdown
    tb = manifest["ai_tokens_breakdown"]
    assert tb["total_tokens"] == 6500
    assert tb["research_tokens"] == 1500
    assert tb["generation_tokens"] == 3000
    assert tb["audit_tokens"] == 800
    assert tb["repair_tokens"] == 1200
    # 300 seconds = 5.0 minutes. 6500 / 5.0 = 1300.0 tokens/min
    assert tb["ai_tokens_per_audio_minute"] == 1300.0
