"""
Comprehensive Unit and Regression Tests for Post-Generation AI Capability Routing,
Structured Output Execution, Provider Registry Consistency, and Truthful Telemetry.
"""

from unittest.mock import MagicMock, patch
import pytest

from herald.ai.base import AIProvider, ProviderCapabilities
from herald.ai.capabilities import VALID_CAPABILITIES, validate_capability
from herald.ai.errors import AIModelUnavailableError, AIProviderError, AIUnsupportedCapabilityError
from herald.ai.failover import execute_with_failover
from herald.ai.gemini_provider import GeminiProvider
from herald.ai.long_form import (
    EvidenceScope,
    audit_and_repair_fidelity,
    cleanup_script_metadata,
    expand_script_content_gap,
    review_script_repetition,
)
from herald.ai.registry import list_descriptors
from herald.ai.schema import (
    MetadataCleanupResponse,
    MetadataSectionHeading,
    PodcastScriptResponse,
    PodcastSegment,
    RepetitionReviewItem,
    RepetitionReviewResponse,
)
from herald.db.models import AIInteraction, PodcastJob
from herald.services.diagnostics_export import build_manifest_dict


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.count.return_value = 0
    return db


@pytest.fixture(autouse=True)
def mock_all_providers_configured():
    with patch("herald.ai.failover.is_provider_configured", return_value=True):
        yield



def _create_job(chain, failover_index=0, job_id="job-test", custom_title=None):
    return PodcastJob(
        id=job_id,
        transport="telegram",
        status="RECEIVED",
        ai_provider=chain[0]["provider"] if chain else "gemini",
        ai_model=chain[0]["model"] if chain else "gemini-3.5-flash",
        ai_provider_chain_json=chain,
        ai_failover_index=failover_index,
        custom_title=custom_title,
    )


# ==============================================================================
# Suite A: Gemini Sole-Provider Post-Generation Happy Path
# ==============================================================================

def test_gemini_sole_provider_reaches_all_post_gen_operations(mock_db):
    """
    Assert that a sole Gemini provider configured on a job directly reaches
    provider callbacks for all 5 post-generation operations without being skipped.
    """
    job = _create_job([{"provider": "gemini", "model": "gemini-3.5-flash"}], job_id="job-gemini-happy")

    # 1. Structured repetition review -> generate_structured_output
    review_resp = RepetitionReviewResponse(
        has_substantive_repetition=True,
        reviews=[
            RepetitionReviewItem(
                section_b=2,
                section_a=1,
                concept_or_passage="repeated concept",
                is_substantive_repetition=True,
                explanation="redundant explanation",
                passage_to_repair="bad repetition in B",
                passage_a="original explanation in A",
            )
        ],
    )

    fake_gemini = MagicMock(spec=GeminiProvider)
    fake_gemini.provider_name = "Gemini"
    fake_gemini.configured_model = "gemini-3.5-flash"
    fake_gemini.capabilities = ProviderCapabilities(
        script_brief=True,
        script_standard=True,
        structured_output=True,
        research_grounding=True,
        verification=True,
    )
    fake_gemini.is_configured.return_value = True
    fake_gemini.generate_structured_output.return_value = review_resp

    sections = [
        {"section_index": 1, "heading": "Part 1", "narration": "Original explanation.", "word_count": 100},
        {"section_index": 2, "heading": "Part 2", "narration": "bad repetition in B.", "word_count": 100},
    ]
    near_dups = [{"metadata": {"section_a": 1, "section_b": 2, "passage_a": "Original", "passage_b": "bad repetition in B", "similarity": 0.85}}]

    with patch("herald.ai.failover.create_provider", return_value=fake_gemini):
        rev_res, to_repair, rep_meta = review_script_repetition(
            job=job,
            completed_sections=sections,
            near_duplicate_warnings=near_dups,
            distinctive_phrase_warnings=[],
            topic="Testing",
            db=mock_db,
        )

    assert rev_res is not None
    assert rev_res.has_substantive_repetition is True
    assert fake_gemini.generate_structured_output.called
    assert job.ai_failover_index == 0

    # 2. Metadata cleanup -> generate_structured_output
    meta_cleanup_resp = MetadataCleanupResponse(
        episode_title="Refined Title",
        headings=[
            MetadataSectionHeading(order=1, heading="Introduction"),
            MetadataSectionHeading(order=2, heading="Deep Dive"),
        ],
    )
    fake_gemini.generate_structured_output.return_value = meta_cleanup_resp
    script_dict = {
        "episode_title": "Old Title",
        "segments": [
            {"order": 1, "heading": "Part 1", "narration": "Narration 1"},
            {"order": 2, "heading": "Part 2", "narration": "Narration 2"},
        ],
    }

    with patch("herald.ai.failover.create_provider", return_value=fake_gemini):
        cleaned_script, clean_meta = cleanup_script_metadata(
            job=job,
            script_dict=script_dict,
            topic="Testing",
            scope=EvidenceScope.SOURCE_ONLY,
            db=mock_db,
            return_metadata=True,
        )

    assert cleaned_script["episode_title"] == "Refined Title"
    assert cleaned_script["segments"][0]["heading"] == "Introduction"
    assert cleaned_script["segments"][1]["heading"] == "Deep Dive"
    assert clean_meta["performed"] is True
    assert job.ai_failover_index == 0

    # 3. Duplicate narration repair -> generate_script
    repaired_script_resp = PodcastScriptResponse(
        episode_title="Testing",
        episode_description="Clean",
        segments=[PodcastSegment(order=1, heading="Deep Dive", narration="Fresh clean prose without repetition.")],
        warnings=[],
    )
    fake_gemini.generate_script.return_value = repaired_script_resp

    def _exec_dup(p_inst, att, src):
        return p_inst.generate_script(
            source_text=src,
            request_mode="standard",
            source_title="Testing",
            job_id=job.id,
            is_isolated_section=True,
            operation="duplicate_repair",
        )

    with patch("herald.ai.failover.create_provider", return_value=fake_gemini):
        dup_res = execute_with_failover(
            job=job,
            operation="duplicate_repair",
            execute_fn=_exec_dup,
            db=mock_db,
            source_text="source data",
        )
    assert dup_res is not None
    assert fake_gemini.generate_script.called
    assert job.ai_failover_index == 0

    # 4. Targeted gap research -> generate_grounded_research
    fake_gemini.generate_grounded_research.return_value = {
        "raw_text": "Discovered new facts",
        "grounding_metadata": {"webSearchQueries": ["gap search query"]},
        "sources": [{"title": "Grounded Source", "url": "https://example.com/source"}],
    }

    def _exec_supp(p_inst, att, src):
        return p_inst.generate_grounded_research(
            source_text="gap research prompt",
            research_depth="low",
            job_id=job.id,
        )

    with patch("herald.ai.failover.create_provider", return_value=fake_gemini):
        supp_res = execute_with_failover(
            job=job,
            operation="supplemental_research",
            execute_fn=_exec_supp,
            db=mock_db,
            source_text="gap topic",
            required_capability="research_grounding",
        )
    assert supp_res is not None
    assert fake_gemini.generate_grounded_research.called
    assert job.ai_failover_index == 0

    # 5. Semantic fidelity audit -> audit_script_fidelity
    mock_audit = MagicMock()
    mock_audit.has_material_issues = False
    mock_audit.model_dump.return_value = {"has_material_issues": False}
    fake_gemini.audit_script_fidelity.return_value = mock_audit

    with patch("herald.ai.failover.create_provider", return_value=fake_gemini):
        _, audit_meta = audit_and_repair_fidelity(
            job=job,
            sections=sections,
            source_ledger={"clean_text": "Source context"},
            evidence_packet={},
            scope=EvidenceScope.SOURCE_ONLY,
            db=mock_db,
        )
    assert audit_meta["status"] == "clean"
    assert audit_meta["audit_executed"] is True
    assert fake_gemini.audit_script_fidelity.called
    assert job.ai_failover_index == 0


# ==============================================================================
# Suite B: Capability Mapping and Validation
# ==============================================================================

def test_validate_capability_rejects_unknown_capabilities():
    """validate_capability must fail fast on unknown capabilities like 'script_generation'."""
    with pytest.raises(ValueError, match="Unknown AI provider capability 'script_generation'"):
        validate_capability("script_generation")

    with pytest.raises(ValueError, match="Unknown AI provider capability 'voice_synthesis'"):
        validate_capability("voice_synthesis")

    with pytest.raises(ValueError, match="Unknown AI provider capability ''"):
        validate_capability("")

    # Valid capabilities must succeed without raising
    for cap in VALID_CAPABILITIES:
        validate_capability(cap)


def test_execute_with_failover_fails_fast_on_invalid_capability(mock_db):
    """execute_with_failover must raise ValueError immediately before touching job state."""
    job = _create_job([{"provider": "gemini", "model": "gemini-3.5-flash"}], job_id="job-val-cap")

    with pytest.raises(ValueError, match="Unknown AI provider capability 'script_generation'"):
        execute_with_failover(
            job=job,
            operation="repetition_review",
            execute_fn=lambda p, a, s: None,
            db=mock_db,
            required_capability="script_generation",
        )

    # Job failover cursor must not have moved
    assert job.ai_failover_index == 0


# ==============================================================================
# Suite C: Heterogeneous Failover Chain Scenarios
# ==============================================================================

def _make_mock_provider(name, model, capabilities):
    p = MagicMock(spec=AIProvider)
    p.provider_name = name
    p.configured_model = model
    p.capabilities = capabilities
    p.is_configured.return_value = True
    return p


def test_scenario_1_capability_skip_does_not_advance_durable_cursor(mock_db):
    """
    Scenario 1: Capability skip only.
    Candidate 0 (Groq) lacks research_grounding.
    Candidate 1 (Gemini) has research_grounding and succeeds.
    Durable cursor MUST remain 0, so Candidate 0 remains active for next operation.
    """
    job = _create_job(
        [
            {"provider": "groq", "model": "llama-3.3-70b-versatile"},
            {"provider": "gemini", "model": "gemini-3.5-flash"},
        ],
        job_id="job-scen-1",
    )

    groq = _make_mock_provider("Groq", "llama-3.3-70b-versatile", ProviderCapabilities(research_grounding=False, script_standard=True))
    gemini = _make_mock_provider("Gemini", "gemini-3.5-flash", ProviderCapabilities(research_grounding=True, script_standard=True))

    def _factory(provider_id, model_id=None, **kw):
        return groq if provider_id == "groq" else gemini

    # 1. Operation requiring research_grounding: Groq skipped, Gemini executes
    with patch("herald.ai.failover.create_provider", side_effect=_factory):
        res = execute_with_failover(
            job=job,
            operation="supplemental_research",
            execute_fn=lambda p, a, s: {"evidence": "found"},
            db=mock_db,
            required_capability="research_grounding",
        )
    assert res == {"evidence": "found"}
    assert job.ai_failover_index == 0  # Invariant: durable cursor NOT advanced!
    assert job.ai_effective_provider == "gemini"  # Observability reflects who ran it

    # 2. Next operation requiring standard script generation: Groq is still candidate 0 and executes!
    groq.generate_script.return_value = PodcastScriptResponse(
        episode_title="Title", episode_description="Desc", segments=[PodcastSegment(order=1, heading="H", narration="N")], warnings=[]
    )
    with patch("herald.ai.failover.create_provider", side_effect=_factory):
        res2 = execute_with_failover(
            job=job,
            operation="duplicate_repair",
            execute_fn=lambda p, a, s: p.generate_script(s),
            db=mock_db,
        )
    assert res2 is not None
    assert groq.generate_script.called
    assert job.ai_failover_index == 0
    assert job.ai_effective_provider == "groq"


def test_scenario_2_real_failure_advances_cursor_and_sticks(mock_db):
    """
    Scenario 2: Real failure only.
    Candidate 0 (Groq) experiences genuine error (e.g. rate limited / 500).
    Candidate 1 (Gemini) succeeds.
    Durable cursor MUST advance to 1 and stick.
    """
    job = _create_job(
        [
            {"provider": "groq", "model": "llama-3.3-70b-versatile"},
            {"provider": "gemini", "model": "gemini-3.5-flash"},
        ],
        job_id="job-scen-2",
    )

    groq = _make_mock_provider("Groq", "llama-3.3-70b-versatile", ProviderCapabilities(script_standard=True))
    gemini = _make_mock_provider("Gemini", "gemini-3.5-flash", ProviderCapabilities(script_standard=True))

    groq_called = False

    def _exec_script(p, a, s):
        nonlocal groq_called
        if p.provider_name == "Groq":
            groq_called = True
            raise AIModelUnavailableError("Groq model retired", provider="groq")
        return "Gemini success"

    def _factory(provider_id, model_id=None, **kw):
        return groq if provider_id == "groq" else gemini

    with patch("herald.ai.failover.create_provider", side_effect=_factory):
        res = execute_with_failover(
            job=job,
            operation="script_generation",
            execute_fn=_exec_script,
            db=mock_db,
        )

    assert res == "Gemini success"
    assert groq_called is True
    assert job.ai_failover_index == 1
    assert job.ai_effective_provider == "gemini"

    # Future operations must now start directly at Gemini
    groq_called_again = False

    def _exec_next(p, a, s):
        nonlocal groq_called_again
        if p.provider_name == "Groq":
            groq_called_again = True
        return "Gemini next success"

    with patch("herald.ai.failover.create_provider", side_effect=_factory):
        res_next = execute_with_failover(
            job=job,
            operation="script_generation",
            execute_fn=_exec_next,
            db=mock_db,
        )
    assert res_next == "Gemini next success"
    assert groq_called_again is False  # Groq was skipped because cursor stuck at 1!


def test_scenario_3_real_failure_then_capability_skip_then_success(mock_db):
    """
    Scenario 3: Mixed failover:
    Primary 0 (Groq) genuinely fails.
    Secondary 1 (Mistral) lacks research_grounding.
    Tertiary 2 (Gemini) has research_grounding and succeeds.
    Durable cursor MUST advance past genuinely failed Groq (idx 0 -> 1).
    It must NOT skip past Mistral, because Mistral didn't fail genuinely!
    """
    job = _create_job(
        [
            {"provider": "groq", "model": "llama-3.3-70b-versatile"},
            {"provider": "mistral", "model": "mistral-large-latest"},
            {"provider": "gemini", "model": "gemini-3.5-flash"},
        ],
        job_id="job-scen-3",
    )

    groq = _make_mock_provider("Groq", "m0", ProviderCapabilities(research_grounding=True))
    mistral = _make_mock_provider("Mistral", "m1", ProviderCapabilities(research_grounding=False, script_standard=True))
    gemini = _make_mock_provider("Gemini", "m2", ProviderCapabilities(research_grounding=True, script_standard=True))

    def _factory(provider_id, model_id=None, **kw):
        if provider_id == "groq":
            return groq
        elif provider_id == "mistral":
            return mistral
        return gemini

    def _exec_research(p, a, s):
        if p.provider_name == "Groq":
            raise AIModelUnavailableError("Groq model unavailable", provider="groq")
        return "Gemini research success"

    def _get_desc(p_id):
        prov = {"groq": groq, "mistral": mistral, "gemini": gemini}[p_id]
        mock_d = MagicMock()
        mock_d.capabilities = prov.capabilities
        return mock_d

    with patch("herald.ai.failover.create_provider", side_effect=_factory), \
         patch("herald.ai.failover.get_descriptor", side_effect=_get_desc):
        res = execute_with_failover(
            job=job,
            operation="supplemental_research",
            execute_fn=_exec_research,
            db=mock_db,
            required_capability="research_grounding",
        )

    assert res == "Gemini research success"
    # Durable cursor stopped at lowest non-failed candidate: Mistral (1)
    assert job.ai_failover_index == 1
    assert job.ai_effective_provider == "gemini"


def test_scenario_4_capability_skip_then_real_failure_then_success(mock_db):
    """
    Scenario 4: Mixed failover reverse order:
    Primary 0 (Groq) lacks research_grounding.
    Secondary 1 (OpenAI) has research_grounding but genuinely fails.
    Tertiary 2 (Gemini) has research_grounding and succeeds.
    Candidate 0 never failed genuinely, so durable cursor stays 0!
    """
    job = _create_job(
        [
            {"provider": "groq", "model": "m0"},
            {"provider": "openai", "model": "m1"},
            {"provider": "gemini", "model": "m2"},
        ],
        job_id="job-scen-4",
    )

    groq = _make_mock_provider("Groq", "m0", ProviderCapabilities(research_grounding=False))
    openai = _make_mock_provider("OpenAI", "m1", ProviderCapabilities(research_grounding=True))
    gemini = _make_mock_provider("Gemini", "m2", ProviderCapabilities(research_grounding=True))

    def _factory(provider_id, model_id=None, **kw):
        if provider_id == "groq":
            return groq
        elif provider_id == "openai":
            return openai
        return gemini

    def _exec_research(p, a, s):
        if p.provider_name == "OpenAI":
            raise AIModelUnavailableError("OpenAI model unavailable", provider="openai")
        return "Gemini research success"

    def _get_desc(p_id):
        prov = {"groq": groq, "openai": openai, "gemini": gemini}[p_id]
        mock_d = MagicMock()
        mock_d.capabilities = prov.capabilities
        return mock_d

    with patch("herald.ai.failover.create_provider", side_effect=_factory), \
         patch("herald.ai.failover.get_descriptor", side_effect=_get_desc):
        res = execute_with_failover(
            job=job,
            operation="supplemental_research",
            execute_fn=_exec_research,
            db=mock_db,
            required_capability="research_grounding",
        )

    assert res == "Gemini research success"
    # Candidate 0 (Groq) never failed, so durable cursor remains 0
    assert job.ai_failover_index == 0
    assert job.ai_effective_provider == "gemini"


def test_scenario_5_multiple_capability_skips_preserve_cursor(mock_db):
    """Multiple capability skips in a row must not advance the cursor."""
    job = _create_job(
        [
            {"provider": "groq", "model": "m0"},
            {"provider": "mistral", "model": "m1"},
            {"provider": "cloudflare", "model": "m2"},
            {"provider": "gemini", "model": "m3"},
        ],
        job_id="job-scen-5",
    )

    p0 = _make_mock_provider("Groq", "m0", ProviderCapabilities(verification=False))
    p1 = _make_mock_provider("Mistral", "m1", ProviderCapabilities(verification=False))
    p2 = _make_mock_provider("Cloudflare", "m2", ProviderCapabilities(verification=False))
    p3 = _make_mock_provider("Gemini", "m3", ProviderCapabilities(verification=True))

    def _factory(provider_id, model_id=None, **kw):
        return {"groq": p0, "mistral": p1, "cloudflare": p2, "gemini": p3}[provider_id]

    with patch("herald.ai.failover.create_provider", side_effect=_factory):
        res = execute_with_failover(
            job=job,
            operation="verification",
            execute_fn=lambda p, a, s: "Verified by Gemini",
            db=mock_db,
            required_capability="verification",
        )

    assert res == "Verified by Gemini"
    assert job.ai_failover_index == 0
    assert job.ai_effective_provider == "gemini"


# ==============================================================================
# Suite D: Provider Registry Consistency
# ==============================================================================

def test_registry_providers_structured_output_contract():
    """
    Every registered provider declaring structured_output=True in its capabilities
    MUST implement generate_structured_output() method.
    """
    descriptors = list_descriptors()
    assert len(descriptors) > 0

    for desc in descriptors:
        provider = desc.factory(desc.default_model)
        if desc.capabilities.structured_output:
            assert hasattr(provider, "generate_structured_output"), (
                f"Provider '{desc.provider_id}' declares structured_output=True but lacks generate_structured_output method"
            )
            assert callable(getattr(provider, "generate_structured_output"))


# ==============================================================================
# Suite E: Duplicate Repair Execution
# ==============================================================================

def test_duplicate_repair_passes_operation_and_uses_generate_script(mock_db):
    """
    Duplicate repair must invoke generate_script() with operation='duplicate_repair'.
    """
    job = _create_job([{"provider": "gemini", "model": "gemini-3.5-flash"}], job_id="job-dup-repair")

    fake_provider = _make_mock_provider("Gemini", "gemini-3.5-flash", ProviderCapabilities(script_standard=True))
    fake_provider.generate_script.return_value = PodcastScriptResponse(
        episode_title="Repaired",
        episode_description="Clean",
        segments=[PodcastSegment(order=1, heading="Cleaned Section", narration="Unrepeated narration content.")],
        warnings=[],
    )

    with patch("herald.ai.failover.create_provider", return_value=fake_provider):
        res = execute_with_failover(
            job=job,
            operation="duplicate_repair",
            execute_fn=lambda p, a, s: p.generate_script(s, operation="duplicate_repair"),
            db=mock_db,
        )

    assert res is not None
    fake_provider.generate_script.assert_called_once()
    assert fake_provider.generate_script.call_args[1].get("operation") == "duplicate_repair"


# ==============================================================================
# Suite F: Targeted Gap Research With Evidence
# ==============================================================================

def test_expand_script_content_gap_records_complete_telemetry(mock_db):
    """
    expand_script_content_gap must record complete telemetry fields in gap_diagnostics.
    """
    job = _create_job([{"provider": "gemini", "model": "gemini-3.5-flash"}], job_id="job-gap-telemetry")

    sections = [
        {"section_index": 1, "heading": "First Section", "purpose": "Intro", "key_points": ["P1"], "narration": "Short text.", "word_count": 50, "relevant_evidence_ids": []}
    ]
    gap_info = {
        "deficit": 400,
        "total_words": 50,
        "planned_target": 450,
        "fill_ratio": 0.11,
        "is_overall_underfilled": True,
        "underfilled_sections": [{"section_index": 1, "target_budget": 450, "actual_words": 50, "deficit": 400}],
    }

    fake_gemini = _make_mock_provider("Gemini", "gemini-3.5-flash", ProviderCapabilities(research_grounding=True, script_standard=True))
    fake_gemini.generate_grounded_research.return_value = {
        "raw_text": "New grounded research facts for section 1",
        "grounding_metadata": {"webSearchQueries": ["search query 1", "search query 2"]},
        "sources": [{"title": "Source 1", "url": "https://example.com/1"}],
    }
    fake_gemini.generate_script.return_value = PodcastScriptResponse(
        episode_title="Expanded",
        episode_description="Clean",
        segments=[PodcastSegment(order=1, heading="First Section", narration="Short text. Expanded with deep analysis from new research facts.")],
        warnings=[],
    )

    with patch("herald.ai.failover.create_provider", return_value=fake_gemini):
        res_secs, gap_meta = expand_script_content_gap(
            job=job,
            completed_sections=sections,
            gap_info=gap_info,
            topic="Space Exploration",
            evidence_packet={"items": []},
            scope=EvidenceScope.RESEARCH,
            db=mock_db,
            return_metadata=True,
        )

    # Verify all required telemetry fields are present and truthful
    assert gap_meta["gap_detected"] is True
    assert gap_meta["triggered"] is True
    assert gap_meta["attempted"] is True
    assert gap_meta["succeeded"] is True
    assert gap_meta["search_count"] == 2
    assert gap_meta["new_evidence_count"] >= 1
    assert 1 in gap_meta["affected_sections"]
    assert gap_meta["failure_category"] is None
    assert gap_meta["supplemental_research"]["succeeded"] is True


# ==============================================================================
# Suite G: Fidelity Audit Truthfulness
# ==============================================================================

def test_fidelity_audit_status_truthfulness_when_skipped_or_failed(mock_db):
    """
    Fidelity audit status must never report 'clean' when audit was skipped or failed.
    """
    job = PodcastJob(id="job-fid-truth", custom_title="Truth Test")
    sections = [{"section_index": 1, "heading": "H", "narration": "Text", "word_count": 10}]

    # 1. When execute_with_failover raises exception (e.g. all providers failed)
    with patch("herald.ai.long_form.execute_with_failover", side_effect=AIProviderError("Audit timeout", provider="gemini")):
        _, res_failed = audit_and_repair_fidelity(
            job=job,
            sections=sections,
            source_ledger={"clean_text": "Source text"},
            evidence_packet={},
            scope=EvidenceScope.SOURCE_ONLY,
            db=mock_db,
        )
    assert res_failed["status"] == "failed_nonfatal"
    assert res_failed["audit_executed"] is False
    assert res_failed["status"] != "clean"

    # 2. When execute_with_failover succeeds and reports no issues
    mock_audit_clean = MagicMock()
    mock_audit_clean.has_material_issues = False
    mock_audit_clean.model_dump.return_value = {"has_material_issues": False}

    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_audit_clean):
        _, res_clean = audit_and_repair_fidelity(
            job=job,
            sections=sections,
            source_ledger={"clean_text": "Source text"},
            evidence_packet={},
            scope=EvidenceScope.SOURCE_ONLY,
            db=mock_db,
        )
    assert res_clean["status"] == "clean"
    assert res_clean["audit_executed"] is True


# ==============================================================================
# Suite H: Metadata Cleanup with MetadataCleanupResponse
# ==============================================================================

def test_cleanup_script_metadata_uses_structured_output_response_schema(mock_db):
    """
    cleanup_script_metadata must invoke generate_structured_output with MetadataCleanupResponse schema.
    """
    job = _create_job([{"provider": "gemini", "model": "gemini-3.5-flash"}], job_id="job-meta-struct")
    script_dict = {
        "episode_title": "Part 1: Draft Title",
        "segments": [
            {"order": 1, "heading": "Part 1 -", "narration": "Narration text unaltered."},
            {"order": 2, "heading": "Part 2:", "narration": "Second section text unaltered."},
        ],
    }

    mock_response = MetadataCleanupResponse(
        episode_title="Polished Astronomy Episode",
        headings=[
            MetadataSectionHeading(order=1, heading="Early Celestial Maps"),
            MetadataSectionHeading(order=2, heading="Telescopic Discoveries"),
        ],
    )

    fake_provider = _make_mock_provider("Gemini", "gemini-3.5-flash", ProviderCapabilities(structured_output=True))
    fake_provider.generate_structured_output.return_value = mock_response

    with patch("herald.ai.failover.create_provider", return_value=fake_provider):
        cleaned_script, meta = cleanup_script_metadata(
            job=job,
            script_dict=script_dict,
            topic="Astronomy",
            scope=EvidenceScope.SOURCE_ONLY,
            db=mock_db,
            return_metadata=True,
        )

    fake_provider.generate_structured_output.assert_called_once()
    call_kwargs = fake_provider.generate_structured_output.call_args[1]
    assert call_kwargs["response_schema"] == MetadataCleanupResponse
    assert call_kwargs["operation"] == "metadata_cleanup"

    assert cleaned_script["episode_title"] == "Polished Astronomy Episode"
    assert cleaned_script["segments"][0]["heading"] == "Early Celestial Maps"
    assert cleaned_script["segments"][1]["heading"] == "Telescopic Discoveries"
    # Narration MUST remain completely unaltered
    assert cleaned_script["segments"][0]["narration"] == "Narration text unaltered."
    assert cleaned_script["segments"][1]["narration"] == "Second section text unaltered."
    assert meta["performed"] is True


# ==============================================================================
# Suite I: AI Accounting and Telemetry
# ==============================================================================

def test_diagnostics_manifest_classifies_repetition_review_under_audit_tokens(mock_db):
    """
    AI token breakdown in diagnostics manifest must classify 'repetition_review'
    under audit_tokens alongside audit and verification.
    """
    job = PodcastJob(
        id="job-tokens-1",
        custom_title="Tokens Test",
        audio_duration_seconds=120.0,
    )

    interactions = [
        AIInteraction(
            job_id=job.id,
            provider="gemini",
            model="gemini-3.5-flash",
            operation="script_generation",
            total_tokens=1000,
            prompt_tokens=800,
            completion_tokens=200,
            success=True,
        ),
        AIInteraction(
            job_id=job.id,
            provider="gemini",
            model="gemini-3.5-flash",
            operation="repetition_review",
            total_tokens=250,
            prompt_tokens=200,
            completion_tokens=50,
            success=True,
        ),
        AIInteraction(
            job_id=job.id,
            provider="gemini",
            model="gemini-3.5-flash",
            operation="duplicate_repair",
            total_tokens=300,
            prompt_tokens=250,
            completion_tokens=50,
            success=True,
        ),
        AIInteraction(
            job_id=job.id,
            provider="gemini",
            model="gemini-3.5-flash",
            operation="supplemental_research",
            total_tokens=400,
            prompt_tokens=300,
            completion_tokens=100,
            success=True,
        ),
        AIInteraction(
            job_id=job.id,
            provider="gemini",
            model="gemini-3.5-flash",
            operation="verification",
            total_tokens=150,
            prompt_tokens=120,
            completion_tokens=30,
            success=True,
        ),
    ]

    mock_db.query(AIInteraction).filter().all.return_value = interactions

    manifest = build_manifest_dict(
        job=job,
        db=mock_db,
        included_files=["manifest.json"],
        truncated_files=[],
    )
    token_breakdown = manifest["ai_tokens_breakdown"]

    assert token_breakdown["total_tokens"] == 2100
    assert token_breakdown["generation_tokens"] == 1000
    # audit_tokens must include repetition_review (250) + verification (150) = 400
    assert token_breakdown["audit_tokens"] == 400
    # repair_tokens must include duplicate_repair (300)
    assert token_breakdown["repair_tokens"] == 300
    # research_tokens must include supplemental_research (400)
    assert token_breakdown["research_tokens"] == 400
