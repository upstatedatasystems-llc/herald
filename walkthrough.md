# Final Pre-Manual-Acceptance Verification & Correction Walkthrough

This document summarizes the changes, bug fixes, and verification results from the final pre-manual-acceptance pass on the Herald codebase.

---

## 1. Summary of Changes

### Item 1: Blocker — `research_plan` Provider Contract
- **Problem**: `AIProvider.generate_grounded_research()` and `GeminiProvider.generate_grounded_research()` did not accept the `research_plan` parameter passed by the unified long-form pipeline, causing runtime `TypeError: unexpected keyword argument 'research_plan'`.
- **Fix**:
  - Updated [`AIProvider.generate_grounded_research`](file:///c:/Users/mikea/Documents/Herald/herald/ai/base.py) to accept `research_plan: dict[str, Any] | None = None`.
  - Updated [`GeminiProvider.generate_grounded_research`](file:///c:/Users/mikea/Documents/Herald/herald/ai/gemini_provider.py) to accept `research_plan` and pass it to `herald.gemini.client.generate_grounded_research`.
  - Added unit regression test [`test_gemini_provider_real_contract_receives_research_plan`](file:///c:/Users/mikea/Documents/Herald/tests/unit/test_long_form_engine.py) using the real `GeminiProvider` class (not a wildcard mock).

### Item 2: Blocker — Authoritative Semantic Fidelity Audit & Bounded Repair
- **Problem**: Script fidelity audit relied on shallow keyword/number matching heuristic without semantic analysis, and the repair pass did not supply the source text or evidence packet.
- **Fix**:
  - In [`herald/ai/long_form.py`](file:///c:/Users/mikea/Documents/Herald/herald/ai/long_form.py) (`audit_and_repair_fidelity`):
    - Replaced heuristic with authoritative provider methods: `p_inst.audit_script_fidelity()` for Source mode, and `p_inst.audit_research_script()` for Expanded and Topic modes.
    - Updated bounded repair call (`p_inst.repair_script_fidelity` / `repair_research_script`) to provide actual source text, research dossier, current script dictionary, and detailed audit repair instructions.
    - Added exactly one bounded re-audit pass following repair.
    - Tracked explicit statuses: `clean`, `issue_detected`, `repair_attempted`, `repair_succeeded`, `unresolved_issue_remains`.
    - Persisted full audit telemetry to `job.fidelity_audit_json`.

### Item 3: Section and Evidence Distribution
- **Problem**: Fixed 11 sections were nominally planned even when evidence only contained 4 chunks, resulting in chunks 4–11 all referencing chunk 4 (`min(i, len(evidence_ids) - 1)`), leading to repetitive scripts.
- **Fix**:
  - In `build_episode_outline` ([`herald/ai/long_form.py`](file:///c:/Users/mikea/Documents/Herald/herald/ai/long_form.py)):
    - Scaled section count down when evidence is limited (`max_supported_secs = min(nominal_sec_count, max(2, len(evidence_ids) * 2))`).
    - Implemented proportional evidence distribution: `(i * len(evidence_ids)) // sec_count` across sections.
    - Replaced repetitive "Part N" titles with distinct narrative perspectives based on focus areas.
  - Added regression test [`test_outline_evidence_distribution_four_chunks_eleven_nominal_sections`](file:///c:/Users/mikea/Documents/Herald/tests/unit/test_long_form_engine.py).

### Item 4: Fixed-Duration Underfill Enforcement
- **Problem**: Staged long-form generation could underfill desired durations when sections stopped naturally.
- **Fix**:
  - In `execute_unified_long_form_pipeline` ([`herald/ai/long_form.py`](file:///c:/Users/mikea/Documents/Herald/herald/ai/long_form.py)):
    - If total generated words are `< 75%` of planned target budget for Expanded/Topic mode: generates a dedicated synthesis section to bridge the gap.
    - For Source mode: executes bounded source elaboration if evidence supports it; otherwise preserves faithful shorter result without hallucinating padding.
    - Records `requested_target_words`, `evidence_supported_target_words`, and `actual_words` in `job.configuration_state_json`.

### Item 5: Legacy Literal Override in Failover
- **Problem**: In [`herald/ai/failover.py`](file:///c:/Users/mikea/Documents/Herald/herald/ai/failover.py), `get_job_provider_chain` and `execute_with_failover` checked `job.request_mode == "literal"` ahead of `job.content_mode`, improperly forcing non-literal jobs into `LiteralProvider`.
- **Fix**:
  - Replaced legacy `job.request_mode == "literal"` checks with canonical `is_literal = (getattr(job, "content_mode", None) == "literal") or (job.content_mode is None and getattr(job, "request_mode", None) == "literal")`.
  - Added regression test [`test_failover_content_mode_precedence_over_legacy_request_mode`](file:///c:/Users/mikea/Documents/Herald/tests/unit/test_long_form_engine.py).

### Item 6: Initial Research Depth Canonicalization
- **Problem**: Pipeline intake set `research_depth = "medium"` unconditionally on interactive jobs or improperly handled legacy request modes.
- **Fix**:
  - In [`herald/core/pipeline.py`](file:///c:/Users/mikea/Documents/Herald/herald/core/pipeline.py):
    - When `content_mode` is `expanded` or `topic`: `research_depth` is canonicalized to explicit directive, user preference, or `"medium"`.
    - When `content_mode` is `source` or `literal`: `research_depth` is set to `None`.
    - Preserved explicit user preferences (`default_mode="brief"` maps to `source` mode with `research_depth=None`).

### Item 7: Clean Duplicate `research_model` ORM Declaration
- **Problem**: In [`herald/db/models.py`](file:///c:/Users/mikea/Documents/Herald/herald/db/models.py), `research_model` was declared twice on `PodcastJob` (lines 140 and 159).
- **Fix**:
  - Removed duplicate declaration at line 159; retained canonical definition from Migration 006 at line 140.
  - Declared `research_provider = Column(String(50), nullable=True)` cleanly.
  - Clarified Migration 019 docstring to state it adds `research_provider` (since `research_model` was introduced in 006).

### Item 8: Research Model/Provider Telemetry Snapshotting
- **Problem**: Telemetry recorded for research grounding could be overwritten if section scripting fell back to secondary providers.
- **Fix**:
  - In `execute_unified_long_form_pipeline` ([`herald/ai/long_form.py`](file:///c:/Users/mikea/Documents/Herald/herald/ai/long_form.py)):
    - Immediately snapshots `job.research_provider` and `job.research_model` following grounded research execution.
    - Subsequent section generation cannot mutate these grounding snapshots.

### Item 9: Literal UI Consistency & Branding Truthfulness
- **Problem**: Literal mode in Telegram config card and branding intro could present confusing duration claims.
- **Fix**:
  - In [`herald/telegram/bot.py`](file:///c:/Users/mikea/Documents/Herald/herald/telegram/bot.py): selecting literal mode locks `job.target_minutes = "auto"` and `job.research_depth = None`. Duration callback alerts user that literal mode reads full source verbatim.
  - In [`herald/telegram/formatters.py`](file:///c:/Users/mikea/Documents/Herald/herald/telegram/formatters.py): renders `[🔒 Length: N/A for Literal (Full Source)]`.
  - In [`herald/audio/branding.py`](file:///c:/Users/mikea/Documents/Herald/herald/audio/branding.py): `render_intro_narration` forces `INTRO_GENERAL_TEMPLATE` (no minute claim) when `content_mode == "literal"`.

### Item 10: Vertical Integration Tests
- **Added**: [`tests/integration/test_interactive_long_form_pipeline.py`](file:///c:/Users/mikea/Documents/Herald/tests/integration/test_interactive_long_form_pipeline.py) covering 4 complete vertical slices:
  - **Test A**: Topic / 20 min / Grounded Research (`test_vertical_slice_topic_mode_20m_research`)
  - **Test B**: Expanded / 10 min / Source + Research (`test_vertical_slice_expanded_mode_10m_research`)
  - **Test C**: Source / 10 min / Source-only (`test_vertical_slice_source_mode_10m_source_only`)
  - **Test D**: Literal / Verbatim / Zero AI (`test_vertical_slice_literal_mode_zero_ai`)

---

## 2. Verification Results

### Automated Test Suites
1. **Compilation Check**:
   ```bash
   python -m compileall -q herald apps migrations tests
   ```
   **Result**: 0 syntax/bytecode errors (Exit Code 0).

2. **Unit Test Suite**:
   ```bash
   uv run pytest tests/unit/ -q
   ```
   **Result**: 621 passed, 0 failed in 238.24s (Exit Code 0).

3. **Integration Test Suite**:
   ```bash
   uv run pytest tests/integration/ -q
   ```
   **Result**: 16 passed, 10 skipped (Postgres container integration tests skipped on SQLite local runner), 0 failed in 13.17s (Exit Code 0).

---

## 3. Acceptance Checklist

- [x] Blocker resolved: `research_plan` contract updated on `AIProvider` and `GeminiProvider`.
- [x] Blocker resolved: Fidelity audit and repair are fully semantic with actual source and evidence supplied.
- [x] Outline evidence distribution proportionally scales sections and maps chunks without repetition.
- [x] Fixed duration underfill strategy bridges gap with synthesis sections or records duration telemetry.
- [x] Mode failover respects `content_mode` over legacy `request_mode`.
- [x] Research depth initialization correctly respects content mode and user defaults.
- [x] Duplicate ORM column declaration removed.
- [x] Research provider/model telemetry snapshotted immediately after grounding.
- [x] Literal mode UI and audio branding suppress duration claims and lock length parameters.
- [x] 4 real vertical integration tests implemented and passing.
- [x] Full test suites passing with 100% clean exit codes.
