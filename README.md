# Herald

[![CI Workflow](https://github.com/upstatedatasystems-llc/herald/actions/workflows/ci.yml/badge.svg)](https://github.com/upstatedatasystems-llc/herald/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Open source · Self-hosted · Podcasts. Your way.**

## Any topic. Your podcast.

Herald is an open-source, self-hosted podcast generation platform controlled through Telegram. Start with an idea, article, pasted text, or forwarded message. Herald can research, write, narrate, and deliver a finished podcast through Telegram on infrastructure you control.

Telegram is the current remote interface; the generation pipeline runs on your server. Herald uses outbound Telegram long polling, so a normal deployment does not require inbound webhooks, a public IP, a domain, or an HTTPS certificate.

Learn more at [upstatedatasystems.com/Herald](https://upstatedatasystems.com/Herald).

---

## What you can send

Herald currently supports four practical starting points:

- **Topic seed** — a subject, question, headline, or short concept for Herald to research and turn into an episode.
- **Article URL** — a public web page that can be adapted as-is or expanded with outside context.
- **Pasted text** — notes, newsletters, copied articles, reports, or other text pasted directly into Telegram.
- **Forwarded Telegram message** — content already in Telegram that you want to use as the source for a podcast.

After intake, Herald presents an interactive configuration card before generation.

## Generation modes

| Mode | What it does |
| --- | --- |
| **Topic** | Researches a subject and builds a research-backed podcast from the topic seed. |
| **Source** | Treats the supplied material as the factual boundary and creates a structured, source-bounded episode. |
| **Expanded** | Uses the submitted source as the anchor and adds bounded external research for context and background. |
| **Literal** | Cleans, chunks, and narrates the supplied text locally without making LLM API calls. |

For AI-assisted modes, target length can be **Auto, 10, 20, 30, 45, or 60 minutes**. Expanded and Topic modes also expose **Low, Medium, or High** research depth. Literal mode reads the full source and therefore does not use a target duration.

## AI without lock-in

Herald treats the model provider as a configurable component rather than the product itself.

Supported provider types are:

- Google Gemini
- Groq
- Cloudflare Workers AI
- OpenAI
- OpenRouter
- Mistral
- Anthropic
- Ollama
- Literal (zero-AI)

A user can configure a **Primary, Secondary, and Tertiary** AI provider chain. Herald snapshots the selected chain into each job and uses deterministic, restart-safe failover when a provider cannot complete the work. Literal may be used as the Primary provider, but it is not used as a Secondary or Tertiary failover candidate.

Provider and model capabilities vary. Use `/settings`, `/models`, and `/ai-check` in Telegram to inspect the configuration available on your installation.

## Local narration

Narration is produced locally with **Kokoro TTS**, then assembled and normalized with **FFmpeg** before Telegram delivery.

The default curated voice catalog includes American and British English voices. Herald can also discover compatible voices exposed by the running Kokoro service and intersect them with the configured allowlist.

---

## Quick start

### 1. Create a Telegram bot

Open Telegram, message [@BotFather](https://t.me/BotFather), run `/newbot`, and copy the HTTP API token.

### 2. Install Herald

On a clean Ubuntu 24.04 LTS host running AMD64 or ARM64:

```bash
curl -fsSL https://raw.githubusercontent.com/upstatedatasystems-llc/herald/main/install.sh | bash
```

The installer:

1. validates Ubuntu 24.04, CPU architecture, and disk headroom;
2. installs Docker Engine and Docker Compose v2 when needed;
3. runs the Herald setup wizard for Telegram and AI provider configuration;
4. starts PostgreSQL, Kokoro, the Herald worker, and the Telegram bot;
5. applies Alembic database migrations;
6. runs installation acceptance checks; and
7. prints a one-time Telegram owner pairing code.

### 3. Pair your Telegram account

Send the pairing command shown by the installer to your bot:

```text
/pair 123456
```

Once paired, send a topic or source and Herald will present the podcast configuration card.

---

## Telegram controls

| Command | Purpose |
| --- | --- |
| `/start` | Quick-start guide and pairing status |
| `/help` | Full usage guide and directive reference |
| `/settings` | Voice, speed, default mode, target length, research depth, provider chain, models, and confirmation preference |
| `/voices` | Browse the curated voice catalog |
| `/models` | Browse supported models for registered providers |
| `/status` | Runtime health, TTS readiness, AI status, queue, disk, and uptime |
| `/ai-check` | Test configured AI provider connections (`/ai_check` is also accepted) |
| `/queue` | View pending and processing jobs |
| `/download [id]` | Retrieve the latest or a specific completed MP3 |
| `/diagnostics [id]` | View job diagnostics and retrieve a redacted support bundle |
| `/logs YYYY-MM-DD [HH:MM]` | Owner-only export of Herald logs and diagnostics from the requested time |
| `/readme` | Send the project README through Telegram |
| `/pair <code>` | Pair the authorized owner account |

Per-request directives such as `Voice:`, `Speed:`, `Title:`, `Mode:`, `Length:`, and `Research:` can be placed at the top of submitted content.

---

## Current architecture

```text
Telegram
   │
   ▼
Telegram bot ───────────────┐
   │                        │
   ▼                        ▼
Intake + configuration   PostgreSQL
   │                        ▲
   ▼                        │
Source extraction / research│
   │                        │
   ▼                        │
AI provider chain (optional)│
   │                        │
   ▼                        │
Podcast script ─────────────┤
   │                        │
   ▼                        │
Herald worker ──────────────┘
   │
   ▼
Kokoro TTS
   │
   ▼
FFmpeg
   │
   ▼
MP3 → Telegram
```

PostgreSQL stores durable job state, queueing, user preferences, provider-chain snapshots, transitions, and recovery metadata. The worker handles generation and local audio production. Terminal jobs produce bounded, redacted diagnostic bundles for troubleshooting.

The default Docker Compose stack contains:

- `postgres`
- `herald-migration`
- `herald-worker`
- `telegram-bot`
- `kokoro`

---

## Operations

Common host-side commands:

```bash
# Validate the installation
./scripts/install_acceptance.sh

# Runtime status
python3 scripts/status.py
docker compose ps

# Logs
docker compose logs -f --tail=100

# Backup database state
./scripts/backup.sh

# Disposable backup/restore verification
make restore-test

# Update to the latest main branch
./install.sh --update
```

Herald also writes bounded persistent application logs under `./logs/`, including rotating worker and Telegram logs plus terminal diagnostic ZIPs under `./logs/diagnostics/`.

See [docs/operations.md](docs/operations.md) for the runbook.

---

## Documentation

- [Architecture](docs/architecture.md)
- [Deployment](docs/deployment.md)
- [AI providers and failover](docs/ai-providers.md)
- [Kokoro and voices](docs/kokoro-setup.md)
- [Operations](docs/operations.md)
- [Backup and restore validation](docs/backup-restore.md)
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Gemini-specific setup](docs/gemini-setup.md)

---

## Privacy posture

Herald is self-hosted, but external services still matter:

- Telegram carries submitted messages, bot controls, and delivered podcast files.
- AI-assisted modes may send source or research material to the configured AI providers.
- Literal mode makes no LLM API calls, while still using Telegram as the remote interface.
- Kokoro speech synthesis and FFmpeg audio processing run locally in the default deployment.

Review [docs/security.md](docs/security.md) and the [Herald Privacy Policy](https://upstatedatasystems.com/Herald/privacy) for more detail.

---

## License

Herald is released under the [MIT License](LICENSE).

Copyright © 2026 Upstate Data Systems LLC.
