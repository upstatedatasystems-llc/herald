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

### 1. Immutable Chain Snapshotting & Sticky Failover
- When a job is ingested at `RECEIVED`/`EXTRACTING`, the user's ordered provider chain (Primary, Secondary, Tertiary) and configured models are resolved and frozen into `podcast_jobs.ai_provider_chain_json`.
- Before the first AI call, preflight limits are verified safely without logging sensitive source text or API keys.
- If a provider encounters a transient failure (429 rate limit, 5xx server error, timeout), bounded same-provider retry occurs.
- If a provider encounters a fatal or exhausted error (401 invalid credentials, 403 forbidden, model unavailable, unresolvable 413), execution fails over deterministically to the next snapshotted candidate.
- Failover is **sticky**: the winning candidate becomes `ai_effective_provider` / `ai_effective_model` and the durable cursor `ai_failover_index` advances in PostgreSQL, persisting across process restarts and worker recoveries.

### 2. Large-Source Bounded Adaptation Engine
- When incoming text exceeds model context windows or provider payload limits (HTTP 413 / `AIContextLimitExceededError`), Herald triggers same-provider adaptation before cursor failover.
- Uses semantic chunking along paragraph and sentence boundaries.
- Produces structured fact-preserving distillations and compiles a coherent research dossier.
- Hierarchical reduction passes are bounded by `AdaptationBudget` (`max_chunks`, `max_reduction_depth`, `max_ai_calls`, `max_elapsed_seconds`).
- Work consumed persists across provider failover without resetting.
- **Literal Mode Guarantee**: Literal mode performs zero external AI calls and zero adaptation mutation.

### 3. Telegram Settings & Restart-Safe Tokens
- `/settings` presents interactive slot menus for Voice, Default Speed, Default Mode, AI Providers, and AI Models.
- Setting a Primary provider automatically compacts empty slots and eliminates duplicates.
- All inline keyboard callbacks use compact, deterministic SHA-256 tokens (`token = sha256(provider_id + "\0" + model_id)[:10]`), remaining under Telegram's 64-byte callback limit and surviving process restarts.

### 4. Vendor-Neutral Core Orchestration
- Core business logic (`herald/core/pipeline.py`, `apps/api/main.py`) contains **zero** direct imports of vendor-specific SDKs or provider modules.
- All interactions flow through normalized interfaces: `execute_with_failover`, `resolve_job_settings`, and typed `AIProviderError` exceptions.

