# Herald Architecture

Herald is an open-source, self-hosted podcast generation platform with a Telegram-first interface. A job can begin with a topic seed, public URL, pasted text, or forwarded Telegram message and end as a narrated MP3 delivered back through Telegram.

The current product supports four generation modes:

- **Topic** — research synthesis from a subject, question, headline, or short prompt.
- **Source** — structured narration bounded to the supplied source.
- **Expanded** — source-anchored narration augmented with bounded external research.
- **Literal** — deterministic source reading with no LLM API calls.

## System overview

```text
[ Telegram user ]
       │
       │ topic / URL / text / forwarded message
       ▼
[ Telegram bot ]
       │
       ├── pairing and authorization
       ├── interactive configuration
       ├── settings / models / voices / diagnostics
       │
       ▼
[ PostgreSQL ]
       │
       ├── job state and queue
       ├── user preferences
       ├── provider-chain snapshots
       ├── state transitions
       └── recovery / diagnostics metadata
       │
       ▼
[ Herald worker ]
       │
       ├── URL extraction and source normalization
       ├── research and evidence planning
       ├── AI provider execution and failover
       ├── long-form scripting and fidelity checks
       ├── TTS chunk planning
       └── audio assembly
       │
       ▼
[ Kokoro TTS ] → [ FFmpeg ] → [ MP3 ]
                              │
                              ▼
                         [ Telegram ]
```

Telegram uses outbound long polling. The normal deployment therefore does not require an inbound webhook, public application port, domain name, or TLS certificate.

## Service boundaries

The default Docker Compose stack contains five services:

| Service | Responsibility |
| --- | --- |
| `postgres` | Durable state, queueing, preferences, transitions, and recovery metadata |
| `herald-migration` | Applies Alembic schema migrations before application services start |
| `telegram-bot` | User interface, pairing, intake, configuration, settings, diagnostics, and delivery controls |
| `herald-worker` | Source processing, research, scripting, TTS orchestration, audio processing, and job recovery |
| `kokoro` | Local Kokoro-FastAPI speech synthesis |

The bot and worker share the Herald work volume and persistent host log directory. PostgreSQL and Kokoro are reachable only on the Compose network unless an operator deliberately exposes them.

## Durable job model

Herald is built around persistent jobs rather than a single long-running request.

Each job records enough information to survive process restarts, including:

- source and normalized intake metadata;
- content mode, target length, and research depth;
- the resolved AI provider/model chain;
- current failover cursor and effective provider;
- generation and fidelity telemetry;
- TTS progress and audio metadata;
- state transitions and terminal diagnostics.

Workers claim queued work through PostgreSQL and use durable transitions rather than relying on process memory. This supports restart-safe processing and avoids duplicate work during normal recovery.

## Interactive configuration

After intake, Telegram presents a configuration card for:

- content mode: Source, Expanded, Topic, or Literal;
- target length: Auto, 10, 20, 30, 45, or 60 minutes;
- research depth: Low, Medium, or High when the mode uses research;
- shortcuts for user defaults and Literal;
- explicit start or cancel.

User defaults are managed through `/settings` and include voice, speed, mode, target length, research depth, confirmation preference, provider slots, and model choices.

## AI provider architecture

Herald has a provider-neutral execution layer. Registered provider types are:

- Gemini
- Groq
- Cloudflare Workers AI
- OpenAI
- OpenRouter
- Mistral
- Anthropic
- Ollama
- Literal

The server supplies credentials and baseline configuration. A paired user can then choose an allowed Primary, Secondary, and Tertiary chain from configured providers.

### Snapshotting and failover

At job creation, the effective provider/model chain is resolved and snapshotted into the job. Credentials are not stored in that snapshot.

Execution follows the snapshotted order:

1. try the current provider/model;
2. perform bounded same-provider retry or adaptation where appropriate;
3. advance to the next configured provider when the normalized failure is eligible for failover;
4. persist the failover cursor so recovery after a restart resumes consistently.

Literal is a special provider with no external LLM dependency. It may be Primary, but it is not used as Secondary or Tertiary failover.

Provider/model capabilities are not assumed to be identical. Model discovery and capability metadata are used by the UI and execution layer where supported.

## Research and long-form generation

Topic and Expanded modes use the long-form engine to build evidence, plan a narrative, allocate section budgets, generate sections, and audit the finished script.

Important invariants include:

- canonical source text is not silently replaced by adapted text;
- research/evidence and generated narration are tracked separately;
- section generation is bounded by requested duration and available evidence;
- Source mode does not invent padding merely to hit a target duration;
- semantic fidelity checks can block a script before TTS when unresolved issues remain;
- long inputs can be adapted within bounded call/time/depth budgets instead of being truncated blindly.

Source mode stays bounded to supplied material. Expanded mode keeps the source as the anchor while allowing outside context. Topic mode treats the initial prompt as a research subject rather than as a source document.

## Local TTS and audio

Kokoro runs as a local service in the default stack. Herald:

1. normalizes narration for speech;
2. applies pronunciation handling and semantic chunk boundaries;
3. synthesizes chunks through Kokoro;
4. inserts deterministic pauses;
5. assembles and normalizes the program with FFmpeg; and
6. delivers the final MP3 through Telegram.

The curated voice catalog currently groups American English and British English voices. Runtime voice discovery can augment the configured catalog when compatible voices are exposed by Kokoro.

Voice preview samples are stored in the shared Herald work volume and can be rebuilt independently of normal podcast generation.

## Observability and diagnostics

Herald keeps bounded persistent logs under `./logs/` and generates sanitized terminal diagnostic bundles for completed, failed, or cancelled jobs.

Diagnostics include execution state, timings, provider telemetry, and sanitized configuration metadata. API keys, Telegram tokens, Authorization headers, and other configured secrets are redacted.

The paired owner can also export a requested time range with:

```text
/logs YYYY-MM-DD [HH:MM]
```

## Security boundaries

Core boundaries are documented in [security.md](security.md). At a high level:

- a one-time owner pairing code controls the Telegram installation;
- only outbound Telegram polling is required;
- submitted URLs pass through SSRF defenses;
- untrusted source material is treated as data, not executable instructions;
- external AI calls are bounded to configured providers;
- credentials stay in server configuration rather than job snapshots;
- Kokoro and FFmpeg remain local in the default deployment.

## Historical note

The original email-to-podcast MVP design predates the current product. Email intake, n8n orchestration, Gmail delivery, and Google Drive are not part of the current default Herald architecture. The current source of truth is the `main` branch, this documentation set, and the Herald product pages at https://upstatedatasystems.com/Herald.
