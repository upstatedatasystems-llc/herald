"""
Unit tests for Priority 3: Graceful Research Degradation.
Tests:
- Expanded mode (SOURCE_PLUS_RESEARCH) degrades gracefully to SOURCE_ONLY when research fails
  and valid article content exists.
- Resets ai_failover_index to 0 for downstream script generation.
- Correctly sets job.research_degraded and job.research_degradation_reason.
- Telegram approval card includes truthful notice.
- Topic mode (RESEARCH with no source article) fails cleanly and does NOT degrade.
"""

from unittest.mock import MagicMock, patch

import pytest

from herald.ai.errors import AIError, AIQuotaExhaustedError
from herald.ai.long_form import EvidenceScope, execute_unified_long_form_pipeline
from herald.db.models import PodcastJob
from herald.telegram.formatters import format_approval


# ==============================================================================
# Expanded Mode Degradation Tests
# ==============================================================================

def test_expanded_mode_degrades_to_source_only_on_research_failure():
    job = PodcastJob(
        id="test-job-expanded-deg-1",
        transport="telegram",
        status="PENDING",
        content_mode="expanded",
        request_mode="standard",
        source_url="https://example.com/article",
        source_text="This is a comprehensive article about artificial intelligence advances in medicine. " * 10,
        ai_provider="gemini",
        ai_model="gemini-3.5-flash",
        ai_failover_index=2,  # Simulated non-zero index during research attempts
    )

    db = MagicMock()
    status_updates = []

    # Mock execute_with_failover:
    # First call (operation="grounded_research") raises AIQuotaExhaustedError
    # Second call (section_generation) succeeds
    call_counts = {"grounded_research": 0, "section_generation": 0}

    def mock_failover(job, operation, execute_fn, **kwargs):
        if operation == "grounded_research":
            call_counts["grounded_research"] += 1
            raise AIQuotaExhaustedError("Prepayment depleted", provider="gemini")
        elif operation == "section_generation":
            call_counts["section_generation"] += 1
            mock_resp = MagicMock()
            mock_seg = MagicMock()
            mock_seg.narration = "This is narration generated entirely from the source article."
            mock_resp.segments = [mock_seg]
            mock_resp.episode_title = "AI in Medicine"
            mock_resp.episode_description = "Episode generated from source text"
            return mock_resp
        elif operation == "verification":
            mock_audit = MagicMock()
            mock_audit.has_material_issues = False
            mock_audit.repair_instructions = None
            mock_audit.model_dump.return_value = {"has_material_issues": False}
            return mock_audit
        return MagicMock()

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover), \
         patch("herald.ai.long_form.record_job_diagnostic_event"):
        res = execute_unified_long_form_pipeline(
            db=db,
            job=job,
            topic="AI in Medicine",
            scope=EvidenceScope.SOURCE_PLUS_RESEARCH,
            target_minutes="auto",
            research_depth="medium",
            source_text=job.source_text,
            status_notifier=lambda msg: status_updates.append(msg),
        )

    # Invariants:
    # 1. Job marked as degraded
    assert job.research_degraded is True
    assert job.research_degradation_reason == "AI_QUOTA_EXHAUSTED"
    # 2. Failover index reset to 0 so script generation gets full provider chain
    assert job.ai_failover_index == 0
    # 3. Grounding data cleared/none
    assert job.research_grounding_json is None
    assert job.research_source_count == 0
    # 4. Notification sent to user
    assert any("Supplemental research unavailable" in s for s in status_updates)
    # 5. Evidence packet reflects SOURCE_ONLY
    ev_packet = job.evidence_packet_json
    assert ev_packet["scope"] == "SOURCE_ONLY"
    for it in ev_packet.get("items", []):
        assert it.get("is_seed_source") is True


def test_telegram_approval_card_includes_degraded_notice():
    job = PodcastJob(
        id="test-job-approval-card-1",
        transport="telegram",
        status="PENDING",
        content_mode="expanded",
        request_mode="standard",
        source_text="Sample source article text",
        custom_title="AI in Medicine",
        research_degraded=True,
        research_degradation_reason="AI_QUOTA_EXHAUSTED",
        ai_provider="gemini",
        ai_model="gemini-3.5-flash",
    )

    script_json = {
        "episode_title": "AI in Medicine",
        "episode_description": "Discussion of medical AI advances",
        "segments": [{"heading": "Intro", "narration": "Welcome to the show."}],
    }

    card_text, reply_markup = format_approval(job, script_json)

    # Invariant: Approval card must clearly inform the user that research degraded to article-only
    assert "Supplemental research was unavailable, so this episode was generated from the supplied article only." in card_text
    assert "AI in Medicine" in card_text
    assert reply_markup is not None


# ==============================================================================
# Topic Mode Clean Failure Tests
# ==============================================================================

def test_topic_mode_fails_cleanly_without_degradation():
    job = PodcastJob(
        id="test-job-topic-fail-1",
        transport="telegram",
        status="PENDING",
        content_mode="topic",
        request_mode="research",
        source_text="",  # Topic mode has no seed source
        ai_provider="gemini",
        ai_model="gemini-3.5-flash",
    )

    db = MagicMock()

    def mock_failover(job, operation, execute_fn, **kwargs):
        if operation == "grounded_research":
            raise AIQuotaExhaustedError("Prepayment depleted", provider="gemini")
        return MagicMock()

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover), \
         patch("herald.ai.long_form.record_job_diagnostic_event") as mock_diag:
        with pytest.raises(AIError) as exc_info:
            execute_unified_long_form_pipeline(
                db=db,
                job=job,
                topic="Quantum Computing",
                scope=EvidenceScope.RESEARCH,
                target_minutes="auto",
                research_depth="medium",
                source_text="",
            )

        # Invariant: Must NOT degrade to source-only because no article exists
        assert job.research_degraded is not True
        # Invariant: Failure must clearly identify research requirement failure
        err_msg = str(exc_info.value)
        assert "research" in err_msg.lower()
        assert "topic" in err_msg.lower()


# ==============================================================================
# Research Degradation Resume & Exception Boundary Tests
# ==============================================================================

def test_research_degraded_resume_enforces_source_only_scope():
    """Verify that a resumed job with research_degraded=True enforces SOURCE_ONLY throughout subsequent stages."""
    job = PodcastJob(
        id="test-job-resume-deg-1",
        transport="telegram",
        status="PENDING",
        content_mode="expanded",
        request_mode="standard",
        source_url="https://example.com/article",
        source_text="This is comprehensive article source text about renewable energy." * 10,
        research_degraded=True,
        research_degradation_reason="AI_QUOTA_EXHAUSTED",
        evidence_packet_json={
            "topic": "Renewable Energy",
            "scope": "SOURCE_PLUS_RESEARCH",  # Legacy/stale packet scope
            "items": [{
                "evidence_id": "ev_seed",
                "title": "Primary Submitted Source",
                "publisher": "User Source Material",
                "snippet": "This is comprehensive article source text about renewable energy.",
                "is_seed_source": True,
            }],
        },
        ai_provider="gemini",
        ai_model="gemini-2.5-flash",
    )

    db = MagicMock()
    executed_operations = []

    def mock_failover(job, operation, execute_fn, **kwargs):
        executed_operations.append(operation)
        if operation == "section_generation":
            mock_resp = MagicMock()
            mock_seg = MagicMock()
            mock_seg.narration = "Renewable energy narration from source article."
            mock_resp.segments = [mock_seg]
            mock_resp.episode_title = "Renewable Energy"
            mock_resp.episode_description = "Generated from source"
            return mock_resp
        elif operation == "verification":
            mock_audit = MagicMock()
            mock_audit.has_material_issues = False
            mock_audit.repair_instructions = None
            mock_audit.model_dump.return_value = {"has_material_issues": False}
            return mock_audit
        return MagicMock()

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover), \
         patch("herald.ai.long_form.record_job_diagnostic_event"):
        res = execute_unified_long_form_pipeline(
            db=db,
            job=job,
            topic="Renewable Energy",
            scope=EvidenceScope.SOURCE_PLUS_RESEARCH,  # Caller passes original scope
            target_minutes="auto",
            research_depth="medium",
            source_text=job.source_text,
        )

    # Invariants:
    # 1. Stored evidence packet scope must be coerced to SOURCE_ONLY
    assert job.evidence_packet_json["scope"] == "SOURCE_ONLY"
    # 2. Resumed stages must NOT execute research_audit or grounded_research
    assert "grounded_research" not in executed_operations
    assert "research_audit" not in executed_operations
    # 3. Pipeline completed successfully
    assert res is not None


def test_programmer_error_during_research_raises_and_never_degrades():
    """Verify that internal programmer errors (TypeError, AttributeError, etc.) raise directly and do not degrade."""
    job = PodcastJob(
        id="test-job-prog-error-1",
        transport="telegram",
        status="PENDING",
        content_mode="expanded",
        request_mode="standard",
        source_url="https://example.com/article",
        source_text="This is a comprehensive article about artificial intelligence advances in medicine. " * 10,
        ai_provider="gemini",
        ai_model="gemini-2.5-flash",
    )

    db = MagicMock()

    def mock_failover_prog_error(job, operation, execute_fn, **kwargs):
        if operation == "grounded_research":
            raise TypeError("generate_grounded_research() got an unexpected keyword argument 'foo'")
        return MagicMock()

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover_prog_error), \
         patch("herald.ai.long_form.record_job_diagnostic_event"):
        with pytest.raises(TypeError) as exc_info:
            execute_unified_long_form_pipeline(
                db=db,
                job=job,
                topic="AI in Medicine",
                scope=EvidenceScope.SOURCE_PLUS_RESEARCH,
                target_minutes="auto",
                research_depth="medium",
                source_text=job.source_text,
            )

    # Invariant: Must re-raise programmer error directly without catching it as graceful degradation
    assert "unexpected keyword argument 'foo'" in str(exc_info.value)
    assert job.research_degraded is not True

