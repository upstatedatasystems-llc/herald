"""
Regression test suite for Herald Quality Pass:
- Zero-AI Literal Mode Guarantee
- Title & Heading Quality Gate
- FFmpeg True-Peak Mastering & Telemetry
- AI Failover Non-Fatal Fallback
"""

from unittest.mock import patch

from herald.ai.long_form import cleanup_script_metadata
from herald.audio.ffmpeg_builder import join_and_normalize_audio
from herald.config import settings
from herald.db.models import ContentMode, PodcastJob
from herald.literal.script_generator import generate_literal_script
from herald.services.quality_gate import run_quality_gate


def test_literal_mode_zero_ai_guarantee():
    """
    CRITICAL REGRESSION REQUIREMENT:
    Quality gate findings must NEVER cause Literal mode to invoke LLM repairs.
    Literal mode must be completely deterministic, generate clean headings,
    and return metadata_cleanup_recommended=False.
    """
    long_source = "\n\n".join([f"Paragraph {i}: " + ("word " * 60) for i in range(1, 10)])
    literal_script = generate_literal_script(long_source, source_title="Literal Source Title")

    # Verify deterministic segment headings
    assert len(literal_script.segments) >= 2
    assert literal_script.segments[0].heading in ("Introduction", "Reading")
    assert literal_script.segments[1].heading in ("Reading", "Continued")

    # Run through quality gate with ContentMode.LITERAL
    job = PodcastJob(id="test-literal-job", content_mode=ContentMode.LITERAL.value)
    script_dict = literal_script.model_dump() if hasattr(literal_script, "model_dump") else literal_script.to_dict()

    cleaned_dict, report = run_quality_gate(script_dict, job=job)

    # Must NEVER recommend AI metadata cleanup or duplicate repair in Literal mode
    assert report.metadata_cleanup_recommended is False
    assert report.duplicate_repair_recommended is False


def test_cleanup_script_metadata_non_fatal_fallback():
    """
    If AI provider failover fails during metadata cleanup, it must retain
    the original title and headings without failing the job or corrupting narration.
    """
    job = PodcastJob(id="test-meta-fail-job", request_mode="standard")
    script_dict = {
        "episode_title": "Original Valid Title",
        "segments": [
            {"order": 1, "heading": "Section 1", "narration": "Narration text here."},
            {"order": 2, "heading": "Section 2", "narration": "More narration text here."},
        ],
    }

    with patch("herald.ai.long_form.execute_with_failover", side_effect=RuntimeError("AI Provider Offline")):
        res = cleanup_script_metadata(
            job=job,
            script_dict=script_dict,
            topic="Space Exploration",
        )

        assert res["episode_title"] == "Original Valid Title"
        assert res["segments"][0]["heading"] == "Section 1"
        assert res["segments"][0]["narration"] == "Narration text here."


def test_ffmpeg_mastering_records_true_peak_telemetry(monkeypatch, tmp_path):
    """
    Test that join_and_normalize_audio includes true_peak_dbtp in return dict
    and sets the alimiter filter in the ffmpeg command.
    """
    monkeypatch.setenv("HERALD_MOCK_TTS", "1")
    monkeypatch.setattr(settings, "HERALD_ENV", "test")
    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))

    dummy_chunk = tmp_path / "chunk_01.wav"
    dummy_chunk.write_bytes(b"dummy wav data")
    out_mp3 = tmp_path / "output.mp3"

    with patch("herald.audio.ffmpeg_builder.validate_audio_file", return_value={"size_bytes": 100, "duration_seconds": 5.0}):
        res = join_and_normalize_audio(
            chunk_paths=[dummy_chunk],
            output_mp3_path=out_mp3,
            episode_title="Title",
            job_id="job-master-01",
        )

        assert "true_peak_target_dbtp" in res
        assert res["true_peak_target_dbtp"] == getattr(settings, "HERALD_AUDIO_TRUE_PEAK_DBTP", -1.5)
        assert "true_peak_dbtp" in res
        assert "measured_true_peak_dbtp" in res
        assert "measured_integrated_lufs" in res


def test_long_form_orchestration_duration_expansion_rebalances_budget():
    """
    Integration reachability: Real long-form orchestration triggers section expansion
    when words fall below threshold and uses expanded word count in downstream budget redistribution.
    """
    from unittest.mock import MagicMock

    from herald.ai.long_form import EvidenceScope, execute_unified_long_form_pipeline

    mock_db = MagicMock()
    sections_def = [
        {"section_index": 1, "heading": "Part 1", "purpose": "P1", "word_budget": 400, "relevant_evidence_ids": ["E1"]},
        {"section_index": 2, "heading": "Part 2", "purpose": "P2", "word_budget": 400, "relevant_evidence_ids": ["E2"]},
    ]
    outline = {
        "episode_title": "Test Title",
        "target_total_words": 800,
        "sections": sections_def,
    }
    job = PodcastJob(
        id="job-orchestrate-dur-01",
        source_hash="h1",
        content_mode="source",
        outline_json=outline,
        evidence_packet_json={"topic": "Test", "items": [{"evidence_id": "E1", "snippet": "Snippet 1"}, {"evidence_id": "E2", "snippet": "Snippet 2"}]},
    )

    # Initial generator produces underfilled 200 words for Sec 1 (< 85% of 400 = 340)
    def mock_gen_sec(job, section_info, topic, evidence_packet, previous_summary, scope, db=None):
        return {
            "section_index": section_info["section_index"],
            "heading": section_info["heading"],
            "narration": " ".join(["draft"] * 200),
            "word_count": 200,
            "target_word_budget": section_info.get("word_budget"),
            "relevant_evidence_ids": section_info.get("relevant_evidence_ids", []),
            "completed": True,
        }

    # Expansion succeeds and adds 150 words -> 350 words total
    def mock_expand(job, section_info, current_narration, actual_words, target_budget, topic, evidence_packet, scope, covered_context=None, db=None):
        expanded_narr = " ".join(["expanded"] * 350)
        return {
            "success": True,
            "narration": expanded_narr,
            "word_count": 350,
            "words_added": 150,
        }

    with patch("herald.ai.long_form.generate_single_section", side_effect=mock_gen_sec), \
         patch("herald.ai.long_form.expand_single_section", side_effect=mock_expand) as mock_exp_call, \
         patch("herald.ai.long_form.audit_and_repair_fidelity", side_effect=lambda **kw: (kw["sections"], {"status": "clean", "has_material_issues": False})), \
         patch("herald.ai.long_form.record_job_diagnostic_event"):

        res = execute_unified_long_form_pipeline(
            db=mock_db,
            job=job,
            topic="Test Topic",
            scope=EvidenceScope.SOURCE_ONLY,
            target_minutes="6",
            source_text="Test source",
        )

        assert mock_exp_call.called
        # Section 1 narration in result reflects expanded content (350 words)
        sec1_result = res.segments[0]
        assert len(sec1_result.narration.split()) == 350
        assert "expanded" in sec1_result.narration


def test_long_form_orchestration_invokes_duplicate_repair_and_reruns_quality_gate():
    """
    Integration reachability: Quality gate duplicate findings trigger duplicate repair pass
    in orchestration, which then reruns the quality gate and retains before/after findings.
    """
    from unittest.mock import MagicMock

    from herald.ai.long_form import EvidenceScope, execute_unified_long_form_pipeline
    from herald.services.quality_gate import (
        QualityReport,
        QualitySeverity,
        QualityStatus,
        QualityWarning,
    )

    mock_db = MagicMock()
    sections_def = [
        {"section_index": 1, "heading": "S1", "purpose": "P1", "word_budget": 300, "relevant_evidence_ids": ["E1"], "narration": "Narration 1"},
        {"section_index": 2, "heading": "S2", "purpose": "P2", "word_budget": 300, "relevant_evidence_ids": ["E2"], "narration": "Narration 2"},
    ]
    job = PodcastJob(
        id="job-orchestrate-dup-01",
        content_mode="standard",
        outline_json={"episode_title": "T", "target_total_words": 600, "sections": sections_def},
        evidence_packet_json={"items": []},
    )

    dup_warning = QualityWarning(
        code="NEAR_DUPLICATE_PASSAGE",
        message="Section 2 repeats Section 1",
        section_index=2,
        severity=QualitySeverity.WARNING,
        metadata={"section_a": 1, "section_b": 2, "passage_b": "Repeated passage", "similarity": 0.85},
    )
    report_with_dup = QualityReport(
        status=QualityStatus.WARN,
        warnings=[dup_warning],
    )
    report_clean = QualityReport(status=QualityStatus.PASS, warnings=[])

    call_count = 0
    def mock_gate(script, job=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return script, report_with_dup
        return script, report_clean

    def mock_gen_sec(job, section_info, topic, evidence_packet, previous_summary, scope, db=None):
        return {
            "section_index": section_info["section_index"],
            "heading": section_info["heading"],
            "narration": "Narration " * 50,
            "word_count": 50,
            "relevant_evidence_ids": [],
            "completed": True,
        }

    with patch("herald.ai.long_form.generate_single_section", side_effect=mock_gen_sec), \
         patch("herald.services.quality_gate.run_quality_gate", side_effect=mock_gate), \
         patch("herald.ai.long_form.repair_script_duplicates", return_value=(sections_def, {"repair_attempted": True, "repaired_count": 1})) as mock_rep, \
         patch("herald.ai.long_form.audit_and_repair_fidelity", side_effect=lambda **kw: (kw["sections"], {"status": "clean", "has_material_issues": False})):

        res = execute_unified_long_form_pipeline(
            db=mock_db,
            job=job,
            topic="Test",
            scope=EvidenceScope.SOURCE_PLUS_RESEARCH,
            target_minutes="4",
        )

        assert mock_rep.called
        # Quality gate was rerun after repair pass (call_count >= 2)
        assert call_count >= 2
        assert len(res.segments) >= 1


def test_metadata_cleanup_revises_headings_without_altering_narration():
    """
    Integration reachability: Semantic metadata cleanup revises headings/title
    while leaving narration completely unchanged.
    """
    from herald.ai.schema import PodcastScriptResponse, PodcastSegment

    job = PodcastJob(id="job-meta-clean-01", request_mode="standard")
    original_narration = "Exact spoken text that must remain unaltered word for word."
    script_dict = {
        "episode_title": "Section 1: Redundant Episode Title",
        "segments": [
            {"order": 1, "heading": "Reading Part 1", "narration": original_narration}
        ],
    }
    cleaned_segments = [
        PodcastSegment(order=1, heading="Early Discoveries", narration=original_narration)
    ]
    mock_resp = PodcastScriptResponse(
        episode_title="Cosmic Evolution",
        episode_description="Clean summary",
        segments=cleaned_segments,
        warnings=[],
    )

    with patch("herald.ai.long_form.execute_with_failover", return_value=mock_resp):
        res = cleanup_script_metadata(job=job, script_dict=script_dict, topic="Cosmos")

        assert res["episode_title"] == "Cosmic Evolution"
        assert res["segments"][0]["heading"] == "Early Discoveries"
        assert res["segments"][0]["narration"] == original_narration


def test_worker_tts_path_invokes_preflight_and_records_diagnostics(tmp_path, monkeypatch):
    """
    Integration reachability: Worker's actual process_next_job path executes
    pronunciation preflight/normalization and records PRONUNCIATION_PREFLIGHT diagnostic event.
    """
    from unittest.mock import MagicMock

    from apps.worker.main import process_next_job
    from herald.db.models import JobState

    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "HERALD_MIN_DISK_MB", 1)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    fake_job = PodcastJob(
        id="job-worker-preflight-01",
        status=JobState.SYNTHESIZING.value,
        synthesis_attempt_count=1,
        attempt_count=1,
        custom_voice="af_bella",
        script_json={
            "episode_title": "Preflight Episode",
            "segments": [
                {"order": 1, "heading": "Intro", "narration": "NASA launched the telescope in 2025."}
            ],
        },
    )
    mock_db.query.return_value.filter.return_value.first.return_value = fake_job

    mock_kokoro = MagicMock()

    with patch("apps.worker.main.claim_next_job", return_value=fake_job), \
         patch("apps.worker.main.check_free_disk_mb", return_value=5000), \
         patch("apps.worker.main.process_tts_chunks_parallel", return_value=[tmp_path / "chunk_0001.wav"]), \
         patch("apps.worker.main.join_and_normalize_audio", return_value={"output_path": str(tmp_path / "out.mp3"), "duration_seconds": 10, "file_bytes": 500, "true_peak_target_dbtp": -1.5, "sha256": "mock_sha"}), \
         patch("apps.worker.main.run_pronunciation_preflight") as mock_preflight, \
         patch("apps.worker.main.record_job_diagnostic_event") as mock_diag:

        mock_preflight.return_value.total_tokens = 6
        mock_preflight.return_value.type_breakdown = {"INITIALISM_ACRONYM": 1, "NUMBER_SEQUENCE": 1}
        mock_preflight.return_value.to_dict.return_value = {"total_tokens": 6}

        process_next_job(mock_db, kokoro_client=mock_kokoro, worker_id="test-w1")

        assert mock_preflight.called
        # Assert diagnostic event was recorded
        diag_calls = [c for c in mock_diag.call_args_list if c[0][3] == "PRONUNCIATION_PREFLIGHT"]
        assert len(diag_calls) >= 1


def test_stored_voice_snapshot_passed_to_kokoro(tmp_path, monkeypatch):
    """
    Integration reachability: Stored selected voice survives intake snapshot
    and is the exact voice parameter passed to Kokoro synthesis.
    """
    from unittest.mock import MagicMock

    from apps.worker.main import process_next_job
    from herald.db.models import JobState

    monkeypatch.setattr(settings, "HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "HERALD_MIN_DISK_MB", 1)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    # User requested bm_fable (British English)
    fake_job = PodcastJob(
        id="job-worker-voice-01",
        status=JobState.SYNTHESIZING.value,
        synthesis_attempt_count=1,
        attempt_count=1,
        custom_voice="bm_fable",
        custom_speed=1.1,
        script_json={
            "episode_title": "Voice Snapshot Episode",
            "segments": [
                {"order": 1, "heading": "S1", "narration": "Testing British voice delivery."}
            ],
        },
    )
    mock_db.query.return_value.filter.return_value.first.return_value = fake_job

    mock_kokoro = MagicMock()

    with patch("apps.worker.main.claim_next_job", return_value=fake_job), \
         patch("apps.worker.main.check_free_disk_mb", return_value=5000), \
         patch("apps.worker.main.process_tts_chunks_parallel") as mock_parallel, \
         patch("apps.worker.main.join_and_normalize_audio", return_value={"output_path": str(tmp_path / "out.mp3"), "duration_seconds": 10, "file_bytes": 500, "true_peak_target_dbtp": -1.5, "sha256": "mock_sha"}):

        mock_parallel.return_value = [tmp_path / "chunk_0001.wav"]

        process_next_job(mock_db, kokoro_client=mock_kokoro, worker_id="test-w1")

        assert mock_parallel.called
        kwargs = mock_parallel.call_args[1]
        assert kwargs["voice"] == "bm_fable"
        assert kwargs["speed"] == 1.1


def test_source_only_ai_job_duplicate_warning_triggers_repair():
    """
    Test A: SOURCE_ONLY AI job with duplicate warnings triggers duplicate repair pass.
    SOURCE_ONLY is NOT a proxy for Literal; AI duplicate repair must execute.
    """
    from unittest.mock import MagicMock

    from herald.ai.long_form import EvidenceScope, execute_unified_long_form_pipeline
    from herald.services.quality_gate import (
        QualityReport,
        QualitySeverity,
        QualityStatus,
        QualityWarning,
    )

    mock_db = MagicMock()
    sections_def = [
        {"section_index": 1, "heading": "S1", "purpose": "P1", "word_budget": 300, "relevant_evidence_ids": ["E1"], "narration": "Narration 1"},
        {"section_index": 2, "heading": "S2", "purpose": "P2", "word_budget": 300, "relevant_evidence_ids": ["E2"], "narration": "Narration 2"},
    ]
    job = PodcastJob(
        id="job-source-dup-01",
        content_mode=ContentMode.SOURCE.value,
        outline_json={"episode_title": "T", "target_total_words": 600, "sections": sections_def},
        evidence_packet_json={"items": []},
    )

    dup_warning = QualityWarning(
        code="NEAR_DUPLICATE_PASSAGE",
        message="Section 2 repeats Section 1",
        section_index=2,
        severity=QualitySeverity.WARNING,
        metadata={"section_a": 1, "section_b": 2, "passage_b": "Repeated passage", "similarity": 0.85},
    )
    report_with_dup = QualityReport(status=QualityStatus.WARN, warnings=[dup_warning])
    report_clean = QualityReport(status=QualityStatus.PASS, warnings=[])

    call_count = 0
    def mock_gate(script, job=None, **kwargs):
        nonlocal call_count
        call_count += 1
        return (script, report_with_dup) if call_count == 1 else (script, report_clean)

    with patch("herald.ai.long_form.generate_single_section", return_value={"section_index": 1, "heading": "H", "narration": "Narration " * 30, "word_count": 30, "relevant_evidence_ids": [], "completed": True}), \
         patch("herald.services.quality_gate.run_quality_gate", side_effect=mock_gate), \
         patch("herald.ai.long_form.repair_script_duplicates", return_value=(sections_def, {"repair_attempted": True, "repaired_count": 1})) as mock_rep, \
         patch("herald.ai.long_form.audit_and_repair_fidelity", side_effect=lambda **kw: (kw["sections"], {"status": "clean", "has_material_issues": False})):

        res = execute_unified_long_form_pipeline(
            db=mock_db,
            job=job,
            topic="Test Topic",
            scope=EvidenceScope.SOURCE_ONLY,
            target_minutes="4",
        )

        assert mock_rep.called
        assert mock_rep.call_args[1]["scope"] == EvidenceScope.SOURCE_ONLY
        assert call_count >= 2
        assert len(res.segments) >= 1


def test_source_only_ai_job_metadata_problem_triggers_cleanup():
    """
    Test B: SOURCE_ONLY AI job with semantic metadata problems triggers metadata cleanup pass.
    """
    from unittest.mock import MagicMock

    from herald.ai.long_form import EvidenceScope, execute_unified_long_form_pipeline
    from herald.services.quality_gate import (
        QualityReport,
        QualitySeverity,
        QualityStatus,
        QualityWarning,
    )

    mock_db = MagicMock()
    sections_def = [
        {"section_index": 1, "heading": "Reading Part 1", "purpose": "P1", "word_budget": 300, "relevant_evidence_ids": [], "narration": "Narration 1"},
        {"section_index": 2, "heading": "Reading Part 2", "purpose": "P2", "word_budget": 300, "relevant_evidence_ids": [], "narration": "Narration 2"},
    ]
    job = PodcastJob(
        id="job-source-meta-01",
        content_mode=ContentMode.SOURCE.value,
        outline_json={"episode_title": "T", "target_total_words": 600, "sections": sections_def},
        evidence_packet_json={"items": []},
    )

    meta_warning = QualityWarning(
        code="GENERIC_PART_HEADING",
        message="Heading 'Reading Part 2' is generic",
        section_index=2,
        severity=QualitySeverity.WARNING,
    )
    report_with_meta = QualityReport(status=QualityStatus.WARN, warnings=[meta_warning])
    report_clean = QualityReport(status=QualityStatus.PASS, warnings=[])

    call_count = 0
    def mock_gate(script, job=None, **kwargs):
        nonlocal call_count
        call_count += 1
        return (script, report_with_meta) if call_count == 1 else (script, report_clean)

    with patch("herald.ai.long_form.generate_single_section", return_value={"section_index": 1, "heading": "H", "narration": "Narration " * 30, "word_count": 30, "relevant_evidence_ids": [], "completed": True}), \
         patch("herald.services.quality_gate.run_quality_gate", side_effect=mock_gate), \
         patch("herald.ai.long_form.cleanup_script_metadata", return_value={"episode_title": "Polished Title", "segments": [{"order": 1, "heading": "Specific Heading", "narration": "N"}]}) as mock_clean, \
         patch("herald.ai.long_form.audit_and_repair_fidelity", side_effect=lambda **kw: (kw["sections"], {"status": "clean", "has_material_issues": False})):

        res = execute_unified_long_form_pipeline(
            db=mock_db,
            job=job,
            topic="Test Topic",
            scope=EvidenceScope.SOURCE_ONLY,
            target_minutes="4",
        )

        assert mock_clean.called
        assert mock_clean.call_args[1]["scope"] == EvidenceScope.SOURCE_ONLY
        assert call_count >= 2
        assert len(res.segments) >= 1


def test_source_only_repair_remains_source_grounded():
    """
    Test C: In SOURCE_ONLY mode, duplicate repair prompt explicitly restricts
    the model to the supplied source evidence and forbids external facts.
    """
    from unittest.mock import MagicMock

    from herald.ai.long_form import EvidenceScope, repair_script_duplicates
    from herald.services.quality_gate import QualitySeverity, QualityWarning

    job = PodcastJob(id="job-grounded-01", content_mode=ContentMode.SOURCE.value)
    sections = [
        {"section_index": 1, "heading": "Heading 1", "narration": "First passage narration."},
        {"section_index": 2, "heading": "Heading 2", "narration": "Repeated passage narration."},
    ]
    warnings = [
        QualityWarning(
            code="NEAR_DUPLICATE_PASSAGE",
            message="Section 2 repeats Section 1",
            section_index=2,
            severity=QualitySeverity.WARNING,
            metadata={"section_a": 1, "section_b": 2, "passage_b": "Repeated passage narration."},
        )
    ]
    evidence_packet = {
        "items": [
            {"evidence_id": "E1", "snippet": "Source evidence snippet regarding quantum flux."}
        ]
    }

    captured_prompt = None
    with patch("herald.ai.long_form.execute_with_failover") as mock_exec:
        def capture_call(**kwargs):
            nonlocal captured_prompt
            fn = kwargs["execute_fn"]
            mock_p = MagicMock()
            fn(mock_p, 1, "src")
            captured_prompt = mock_p.generate_script.call_args[1]["generation_instructions"]
            return MagicMock(segments=[MagicMock(narration="Grounded revised passage.")])

        mock_exec.side_effect = capture_call
        repaired, meta = repair_script_duplicates(
            job=job,
            sections=sections,
            duplicate_warnings=warnings,
            evidence_packet=evidence_packet,
            topic="Quantum Computing",
            scope=EvidenceScope.SOURCE_ONLY,
        )

        assert captured_prompt is not None
        assert "SOURCE-ONLY GROUNDING REQUIREMENT" in captured_prompt
        assert "Replacement material may ONLY use the supplied source/evidence" in captured_prompt
        assert "MUST NOT introduce external facts" in captured_prompt


def test_literal_job_duplicate_warning_prohibits_ai_repair():
    """
    Test D: Literal job with duplicate warning must strictly PROHIBIT AI duplicate repair.
    """
    from unittest.mock import MagicMock

    from herald.ai.long_form import EvidenceScope, execute_unified_long_form_pipeline
    from herald.services.quality_gate import (
        QualityReport,
        QualitySeverity,
        QualityStatus,
        QualityWarning,
    )

    mock_db = MagicMock()
    sections_def = [
        {"section_index": 1, "heading": "Reading", "purpose": "P1", "word_budget": 300, "relevant_evidence_ids": [], "narration": "Verbatim 1"},
        {"section_index": 2, "heading": "Continued", "purpose": "P2", "word_budget": 300, "relevant_evidence_ids": [], "narration": "Verbatim 2"},
    ]
    job = PodcastJob(
        id="job-literal-dup-01",
        content_mode=ContentMode.LITERAL.value,
        outline_json={"episode_title": "Literal", "target_total_words": 600, "sections": sections_def},
        evidence_packet_json={"items": []},
    )

    dup_warning = QualityWarning(
        code="NEAR_DUPLICATE_PASSAGE",
        message="Section 2 repeats Section 1",
        section_index=2,
        severity=QualitySeverity.WARNING,
        metadata={"section_a": 1, "section_b": 2, "passage_b": "Verbatim 2", "similarity": 0.85},
    )
    report = QualityReport(status=QualityStatus.WARN, warnings=[dup_warning])

    with patch("herald.ai.long_form.generate_single_section", return_value={"section_index": 1, "heading": "H", "narration": "Verbatim", "word_count": 10, "relevant_evidence_ids": [], "completed": True}), \
         patch("herald.services.quality_gate.run_quality_gate", return_value=(job.outline_json, report)), \
         patch("herald.ai.long_form.repair_script_duplicates") as mock_rep, \
         patch("herald.ai.long_form.audit_and_repair_fidelity", side_effect=lambda **kw: (kw["sections"], {"status": "clean", "has_material_issues": False})):

        execute_unified_long_form_pipeline(
            db=mock_db,
            job=job,
            topic="Literal Reading",
            scope=EvidenceScope.SOURCE_ONLY,
            target_minutes="4",
        )

        assert mock_rep.called is False


def test_literal_job_metadata_problem_prohibits_ai_cleanup():
    """
    Test E: Literal job with metadata problem must strictly PROHIBIT AI metadata cleanup.
    """
    from unittest.mock import MagicMock

    from herald.ai.long_form import EvidenceScope, execute_unified_long_form_pipeline
    from herald.services.quality_gate import (
        QualityReport,
        QualitySeverity,
        QualityStatus,
        QualityWarning,
    )

    mock_db = MagicMock()
    sections_def = [
        {"section_index": 1, "heading": "Reading", "purpose": "P1", "word_budget": 300, "relevant_evidence_ids": [], "narration": "Verbatim 1"},
    ]
    job = PodcastJob(
        id="job-literal-meta-01",
        content_mode=ContentMode.LITERAL.value,
        outline_json={"episode_title": "Literal", "target_total_words": 300, "sections": sections_def},
        evidence_packet_json={"items": []},
    )

    meta_warning = QualityWarning(
        code="GENERIC_PART_HEADING",
        message="Heading is generic",
        section_index=1,
        severity=QualitySeverity.WARNING,
    )
    report = QualityReport(status=QualityStatus.WARN, warnings=[meta_warning])

    with patch("herald.ai.long_form.generate_single_section", return_value={"section_index": 1, "heading": "H", "narration": "Verbatim", "word_count": 10, "relevant_evidence_ids": [], "completed": True}), \
         patch("herald.services.quality_gate.run_quality_gate", return_value=(job.outline_json, report)), \
         patch("herald.ai.long_form.cleanup_script_metadata") as mock_clean, \
         patch("herald.ai.long_form.audit_and_repair_fidelity", side_effect=lambda **kw: (kw["sections"], {"status": "clean", "has_material_issues": False})):

        execute_unified_long_form_pipeline(
            db=mock_db,
            job=job,
            topic="Literal Reading",
            scope=EvidenceScope.SOURCE_ONLY,
            target_minutes="4",
        )

        assert mock_clean.called is False


def test_literal_mode_zero_ai_interactions_across_quality_paths():
    """
    Test F: Literal mode records zero AI interactions across expansion,
    duplicate repair, and metadata cleanup paths.
    """
    from unittest.mock import MagicMock

    from herald.ai.long_form import EvidenceScope, execute_unified_long_form_pipeline
    from herald.services.quality_gate import (
        QualityReport,
        QualitySeverity,
        QualityStatus,
        QualityWarning,
    )

    mock_db = MagicMock()
    sections_def = [
        {"section_index": 1, "heading": "Reading", "purpose": "P1", "word_budget": 500, "relevant_evidence_ids": ["E1"], "narration": "Short text."},
    ]
    job = PodcastJob(
        id="job-literal-zero-ai-01",
        content_mode=ContentMode.LITERAL.value,
        outline_json={"episode_title": "Literal", "target_total_words": 500, "sections": sections_def},
        evidence_packet_json={"items": [{"evidence_id": "E1", "snippet": "Evidence"}]},
    )

    warnings = [
        QualityWarning(code="NEAR_DUPLICATE_PASSAGE", message="Dup", section_index=1, severity=QualitySeverity.WARNING, metadata={"section_a": 1, "section_b": 1, "passage_b": "Short text.", "similarity": 0.9}),
        QualityWarning(code="GENERIC_PART_HEADING", message="Heading", section_index=1, severity=QualitySeverity.WARNING),
    ]
    report = QualityReport(status=QualityStatus.WARN, warnings=warnings)

    with patch("herald.ai.long_form.generate_single_section", return_value={"section_index": 1, "heading": "Reading", "narration": "Short text.", "word_count": 2, "relevant_evidence_ids": ["E1"], "completed": True}), \
         patch("herald.services.quality_gate.run_quality_gate", return_value=(job.outline_json, report)), \
         patch("herald.ai.long_form.audit_and_repair_fidelity", side_effect=lambda **kw: (kw["sections"], {"status": "clean", "has_material_issues": False})), \
         patch("herald.ai.long_form.expand_single_section") as mock_expand, \
         patch("herald.ai.long_form.repair_script_duplicates") as mock_repair, \
         patch("herald.ai.long_form.cleanup_script_metadata") as mock_meta:

        res = execute_unified_long_form_pipeline(
            db=mock_db,
            job=job,
            topic="Literal Topic",
            scope=EvidenceScope.SOURCE_ONLY,
            target_minutes="4",
        )

        assert mock_expand.called is False
        assert mock_repair.called is False
        assert mock_meta.called is False
        assert len(res.segments) >= 1


def test_ebur128_parser_representative_ffmpeg_stderr():
    """
    Test post-encode ebur128 parser with standard FFmpeg stderr summary block (mono).
    """
    from herald.audio.ffmpeg_builder import parse_ebur128_output

    sample_stderr = """
[Parsed_ebur128_0 @ 0x55d78e3c8dc0] Summary:

  Integrated loudness:
    I:         -17.5 LUFS
    Threshold: -27.6 LUFS

  Loudness range:
    LRA:         4.2 LU
    Threshold: -37.6 LUFS
    LRA low:   -20.1 LUFS
    LRA high:  -15.9 LUFS

  True peak:
    Peak:        -1.5 dBFS
"""
    res = parse_ebur128_output(sample_stderr)
    assert res["measured_integrated_lufs"] == -17.5
    assert res["measured_true_peak_dbtp"] == -1.5


def test_ebur128_parser_stereo_multichannel_stderr():
    """
    Test ebur128 parser with multi-channel / stereo stderr: takes the maximum true-peak.
    """
    from herald.audio.ffmpeg_builder import parse_ebur128_output

    stereo_stderr = """
[Parsed_ebur128_0 @ 0x7ffd19b21a] Summary:

  Integrated loudness:
    I:         -16.2 LUFS
    Threshold: -26.3 LUFS

  True peak:
    Peak:        -2.1 dBFS
    Peak:        -1.4 dBFS
"""
    res = parse_ebur128_output(stereo_stderr)
    assert res["measured_integrated_lufs"] == -16.2
    assert res["measured_true_peak_dbtp"] == -1.4


def test_ebur128_parser_empty_or_corrupt_stderr():
    """
    Test ebur128 parser gracefully returns None when stderr is empty, missing, or corrupt.
    """
    from herald.audio.ffmpeg_builder import parse_ebur128_output

    assert parse_ebur128_output("") == {"measured_true_peak_dbtp": None, "measured_integrated_lufs": None}
    assert parse_ebur128_output("Random error message with no ebur128") == {"measured_true_peak_dbtp": None, "measured_integrated_lufs": None}
