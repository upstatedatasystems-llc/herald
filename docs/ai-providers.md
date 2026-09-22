# AI Providers and Failover

Herald is provider-neutral. The AI provider is a configurable component of the podcast pipeline rather than a hard dependency on one vendor.

## Supported provider types

| Provider | Required server configuration |
| --- | --- |
| Gemini | `GEMINI_API_KEY`, `GEMINI_MODEL`; optional research model via `GEMINI_RESEARCH_MODEL` |
| Groq | `GROQ_API_KEY`, `GROQ_MODEL` |
| Cloudflare Workers AI | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_AI_MODEL` |
| OpenAI | `OPENAI_API_KEY`, `OPENAI_MODEL` |
| OpenRouter | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` |
| Mistral | `MISTRAL_API_KEY`, `MISTRAL_MODEL` |
| Anthropic | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` |
| Ollama | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` |
| Literal | No AI credentials |

The canonical list of variables and current default model values is maintained in [`.env.example`](../.env.example).

## Server default chain

The server-level fallback chain is configured with:

```env
AI_PROVIDER="gemini"
AI_SECONDARY_PROVIDER=""
AI_TERTIARY_PROVIDER=""
AI_PROVIDER_TIMEOUT_SECONDS=300
```

The installer can configure any supported provider as Primary and can optionally configure Secondary and Tertiary providers.

Literal is intentionally different: it may be configured as Primary for zero-AI operation, but it cannot be used as Secondary or Tertiary failover.

## User-level configuration

A paired Telegram owner can use `/settings` to choose configured providers and models. The server still controls which credentials actually exist; Telegram settings cannot activate an unconfigured provider.

Useful commands:

```text
/settings
/models
/ai-check
```

`/ai-check` executes fresh connectivity checks against the effective provider chain.

## Job snapshotting

When a job is created, Herald resolves the effective provider/model chain and stores a credential-free snapshot with the job. This keeps an in-progress job stable even if user defaults are changed later.

The snapshot includes the ordered candidates and the durable failover position. Secrets remain in environment configuration and are never copied into the job record.

## Failover behavior

Herald normalizes provider failures into a common error model.

A typical execution path is:

1. call the current provider;
2. perform bounded retry/adaptation when the error is considered recoverable;
3. advance to the next configured candidate when failover is appropriate;
4. persist the new failover position;
5. continue the job using the effective provider/model.

This is intentionally bounded. Herald does not dynamically escape the configured chain or select arbitrary providers.

## Model discovery

Herald has a registry and model catalog for all supported providers. Where a provider exposes a suitable model-listing API, Herald can use live or cached discovery. Otherwise it uses verified catalog metadata and configured model IDs.

The Telegram model browser reflects the models known to the running installation rather than assuming every provider has the same capabilities.

## Modes and provider requirements

- **Source** requires AI unless the user chooses Literal instead.
- **Expanded** requires an AI/research-capable configuration because it adds outside context.
- **Topic** requires an AI/research-capable configuration because it builds an episode from a research subject.
- **Literal** makes no LLM API calls.

Research and grounding behavior can differ by provider/model. Herald tracks provider capability metadata and records the effective provider/model used for generation and research in job diagnostics.

## Local Ollama

Ollama can participate in the same provider chain as hosted services.

Example:

```env
AI_PROVIDER="ollama"
OLLAMA_BASE_URL="http://host.docker.internal:11434"
OLLAMA_MODEL="qwen3.5:9b"
```

The URL must be reachable from the Herald containers. On Linux, a host-local Ollama listener bound only to `127.0.0.1` is not automatically reachable from Docker; configure networking intentionally.

## Literal mode

Literal mode is the lowest-dependency path:

- no LLM API call;
- no external research;
- deterministic cleanup and chunking;
- local Kokoro narration;
- Telegram remains the remote interface and delivery path.

This makes Literal useful both as a reading mode and as a fallback operating posture when external AI service access is intentionally disabled.
