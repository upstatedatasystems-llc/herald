# Herald Architectural Reference

## System Overview

Herald is a podcast automation system optimized for single-core cloud deployments and edge servers. It turns articles, newsletters, notes, and documents into high-quality spoken audio delivered through Telegram (with optional legacy email/n8n support).

Herald operates on a **strictly vendor-neutral AI architecture** supporting 9 providers:
- **Google Gemini** (Full support: Brief, Standard, Google Search Grounded Research, URL Context)
- **Groq Cloud** (Fast Llama 3.3 inference)
- **Cloudflare Workers AI** (Serverless inference with Qwen & Gemma tuning)
- **OpenAI** (GPT-4o, GPT-4o-mini)
- **OpenRouter** (Unified multi-vendor routing)
- **Mistral AI** (Mistral Large & Small)
- **Anthropic** (Claude 3.5 Sonnet)
- **Ollama** (Self-hosted local LLMs)
- **Literal Mode** (Deterministic text cleaning & direct narration with **zero** external AI calls)

---

## Logical Architecture & Multi-Provider Failover

```text
[ Telegram User / Intake ]
         │ (Send URL, text, /settings, /models)
         ▼
  [ Telegram Bot ] ──────► [ PostgreSQL 16 ]
         │                     ▲ (Snapshots immutable provider chain:
         │                     │  Primary -> Secondary -> Tertiary)
         ▼                     │
 [ Job State Engine ] ─────────┤
         │                     │
         ▼                     │
[ Deterministic Failover ] ────┤ (Sticky cursor advancement: ai_failover_index)
  ├─ Provider Primary          │
  ├─ Same-Provider Retry/Adapt │
  ├─ Provider Secondary        │
  └─ Provider Tertiary         │
         │                     ▼
         ▼              [ Herald Worker ]
  [ Podcast Script ]           │ (Claims QUEUED_TTS via SELECT FOR UPDATE SKIP LOCKED)
                               ▼
                      [ Kokoro TTS Engine ]
                               ▼
                      [ FFmpeg Normalizer ]
                               ▼
                      [ Telegram Delivery ]
```

---

## Core Invariants & Architecture Design

### 1. Immutable Chain Snapshotting, Security & Sticky Failover
- When a job is ingested at `RECEIVED`/`EXTRACTING`, the user's ordered provider chain (Primary, Secondary, Tertiary) and configured models are resolved and frozen into `podcast_jobs.generation_settings_json` and `ai_provider_chain_json`.
- **Zero Credentials in Snapshots**: No API keys, tokens, or credentials are ever stored in database snapshots, job configurations, or diagnostic events.
- Before the first AI call, preflight limits (size, token limits, timeouts) are verified safely without logging sensitive source text or API keys.
- **Unified Timeouts**: `AI_PROVIDER_TIMEOUT_SECONDS` defaults to 300 seconds across all external AI provider calls, research grounding, repair operations, and URL context extractions. `GEMINI_TIMEOUT_SECONDS` is preserved solely as a backward-compatibility alias.
- If a provider encounters a transient failure (429 rate limit, 5xx server error, timeout), central bounded same-provider retry occurs within the failover executor.
- If a provider encounters a fatal or exhausted error (401 invalid credentials, 403 forbidden, model unavailable, unresolvable 413), execution fails over deterministically to the next snapshotted candidate.
- Failover is **sticky**: the winning candidate becomes `ai_effective_provider` / `ai_effective_model` and the durable cursor `ai_failover_index` advances in PostgreSQL, persisting across process restarts, worker recoveries, and downstream stages (e.g. from URL context into scripting).
- Unclassified errors, programmer bugs, and `ValueError` result in terminal `FAIL_FINAL` termination without advancing the cursor.

### 2. Large-Source Bounded Adaptation Engine
- When incoming text exceeds model context windows or provider payload limits (HTTP 413 / `AIContextLimitExceededError`), Herald triggers same-provider adaptation before cursor failover.
- Uses semantic chunking along paragraph and sentence boundaries.
- Produces structured fact-preserving distillations using the active provider's model.
- **Canonical Integrity**: `job.source_text` is sacred and is NEVER overwritten by adapted or distilled content; adaptation operates solely on ephemeral working text.
- Hierarchical reduction passes are bounded by `AdaptationBudget` (`max_chunks`, `max_reduction_depth`, `max_ai_calls`, `max_elapsed_seconds`). Adaptation may make several AI requests; consumed budget (`usage.ai_calls`) increments faithfully and persists across provider failovers.
- **Literal Mode Guarantee**: Literal mode performs zero external AI calls and zero adaptation mutation.

### 3. Telegram Invariants, Model Discovery & Restart-Safe Tokens
- `/settings` presents interactive slot menus for Voice, Default Speed, Default Mode, AI Providers, and AI Models.
- **Configured Provider Invariants**:
  - Unconfigured providers (missing server API credentials) CANNOT be activated or persisted.
  - Duplicate provider selections are rejected cleanly without mutating or shifting other slots behind the user's back.
  - Provider chain length is enforced to a maximum of 3 candidates in the persistence layer.
  - **Literal Semantics**: Literal may ONLY be configured as Primary and can never be Secondary or Tertiary. Selecting Literal as Primary sets chain = `["literal"]` and sets default mode to Literal in the same transaction if the user was previously in an AI-required mode.
- **Dynamic Model Discovery & Precedence**:
  - Live/cached discovery (via remote provider listing endpoints: Groq, OpenAI, OpenRouter, Mistral) > Verified Herald catalog metadata > Controlled fallback catalog.
  - Discovery is cached (TTL 300s) to ensure responsiveness and resilience during transient network issues.
- **Model Callback Tokens**:
  - Inline keyboard callbacks use provider-scoped deterministic tokens (`h3:m:set:<provider_id>:<token>`), remaining under Telegram's 64-byte callback limit.
  - Token resolution enforces strict collision rejection: ambiguous matches across models are rejected.

### 4. Vendor-Neutral Core Orchestration & Diagnostic Safety
- Core business logic (`herald/core/pipeline.py`, `apps/api/main.py`, `apps/worker/main.py`) contains **zero** direct imports of vendor-specific SDKs or provider modules.
- Active jobs construct providers strictly from snapshotted candidate specifications rather than global mutable singletons.
- All interactions flow through normalized interfaces: `execute_with_failover`, `resolve_job_settings`, and typed `AIProviderError` exceptions preserving the normalized error taxonomy (`error.category`).
- **Safe Failover Details**: Diagnostic events and error messages redact credentials/tokens, avoid dumping full response payloads or excerpts, and cap detail length to prevent secret leakage.

---

## Historical Design Document Notice

> [!NOTE]
> The original design document `Herald_Email_to_Podcast_Design.docx` is an initial design specification and is **historical / superseded** by the current vendor-neutral, multi-provider failover architecture, PostgreSQL 16 state machine, and Telegram-first workflow documented herein.


