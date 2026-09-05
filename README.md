# Herald — Telegram-First Podcast Automation System

[![CI Workflow](https://github.com/upstatedatasystems-llc/herald/actions/workflows/ci.yml/badge.svg)](https://github.com/upstatedatasystems-llc/herald/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Herald turns articles, newsletters, notes, and documents into high-quality spoken audio podcasts delivered directly through Telegram.

Herald operates behind NATs and firewalls using outbound Telegram long polling without requiring open ports, public IPs, domains, HTTPS certificates, Gmail, Google Drive, Google OAuth, or even an AI API key.

---

## Key Features

- **Telegram-First Interface**: Send an article URL, pasted text, or forwarded message directly to your private Telegram bot and receive the completed MP3 podcast in response.
- **Literal Mode (AI is Optional)**: Functions 100% locally on your host with **zero** LLM API calls, performing deterministic text cleaning, heading preservation, sentence-aware chunking, and Kokoro TTS narration.
- **AI-Powered Modes**: When configured with an AI provider (Google Gemini, Groq Cloud, OpenRouter, Mistral AI, or Cloudflare Workers AI), access `brief`, `standard`, and grounded `research` modes.
- **Automated Bootstrap Installer**: Deploy the entire stack on Ubuntu 24.04 with a single command.
- **Secure Owner Pairing**: Prevents unauthorized access using a single-owner one-time pairing code displayed strictly in server console output / container logs (`/pair <code>`).
- **Outbound Long Polling**: No inbound ports, webhooks, or public IP addresses required.
- **Local Neural Speech Synthesis**: Powered by Kokoro-82M TTS and FFmpeg spoken-word loudness normalization (`loudnorm`).
- **Durable Job Engine**: PostgreSQL-backed state machine with automatic crash recovery, lease renewals, and transport-level idempotency.

---

## Quick Start (Ubuntu 24.04 LTS)

### 1. Create a Telegram Bot
1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow instructions to name your bot.
3. Copy the HTTP API token provided by BotFather.

### 2. Run Bootstrap Installer
On your Ubuntu 24.04 server (AMD64 or ARM64), run:

```bash
curl -fsSL https://raw.githubusercontent.com/upstatedatasystems-llc/herald/main/install.sh | bash
```

*(For pre-release testing on the Phase 2 feature branch:)*
```bash
curl -fsSL https://raw.githubusercontent.com/upstatedatasystems-llc/herald/feature/telegram-phase2-productization/install.sh | bash -s -- --ref feature/telegram-phase2-productization
```

The installer will:
1. Verify Ubuntu 24.04, CPU architecture, and disk headroom.
2. Install Docker Engine and Docker Compose v2 if missing.
3. Prompt for your Telegram Bot Token and chosen AI provider.
4. Launch the Docker Compose stack and run schema migrations.
5. Run automated installation acceptance tests (`scripts/install_acceptance.sh`).
6. Display your one-time owner pairing code.

### 3. Pair Your Telegram Account
1. Open a chat with your Telegram Bot.
2. Send the pairing command displayed in the installer output:
   ```text
   /pair 123456
   ```

---

## How to Use Herald

Once paired, send messages directly to your Telegram bot:

### 1. Standard Podcast (AI Scripted)
```text
https://example.com/ai-breakthrough
```

### 2. Literal Reading (Zero AI)
```text
https://example.com/article
literal
```
or paste raw text:
```text
# Architecture Notes
Distributed consensus algorithms ensure state consistency across replicated nodes...
literal
```

### 3. Concise Brief Episode
```text
https://example.com/morning-news
brief
```

### 4. Deep-Dive Grounded Research (Gemini)
```text
https://example.com/complex-topic
research high
```

### Optional Directives (Top of message or in body)
- `Voice: af_bella` (Available: `af_heart`, `af_bella`, `af_sarah`, `am_adam`, `am_michael`)
- `Speed: 1.1` (0.8x to 1.2x)
- `Title: My Custom Episode Title`

---

## Telegram Bot Commands

| Command | Description |
| :--- | :--- |
| `/start` | Welcome message, quick-start guide, and pairing status |
| `/help` | Complete usage guide and directive reference |
| `/download [id]` | Download completed podcast MP3 as an audio document |
| `/diagnostics [id]` | View job diagnostics card and download redacted support bundle ZIP |
| `/status` | Live runtime health, TTS readiness, AI provider health, queue, disk, and uptime |
| `/ai_check` | Dedicated AI API configuration and connectivity test |
| `/queue` | View pending, scripting, and synthesizing podcast jobs |
| `/settings` | View preferences, default voice selection, and pre-TTS confirmation toggle |
| `/readme` | Send the project `README.md` document |
| `/pair <code>` | Pair your Telegram account as the authorized instance owner |

---

## Management & Operations

Herald includes dedicated operational scripts in `scripts/`:

| Action | Command |
| :--- | :--- |
| **Acceptance Test** | `./scripts/install_acceptance.sh` |
| **System Status** | `python3 scripts/status.py` or `docker compose ps` |
| **Live Logs** | `docker compose logs -f --tail=100` |
| **Backup State** | `./scripts/backup.sh` |
| **Restore State** | `./scripts/restore.sh <backup-dir>` |
| **Warm Reset** | `./scripts/reset-herald.sh --warm` *(resets DB/volumes, keeps .env & images)* |
| **Cold Reset** | `./scripts/reset-herald.sh --cold` *(resets DB/volumes & built images, keeps .env)* |
| **Update Stack** | `./install.sh --update` |
| **Reinstall** | `./install.sh --reinstall` |

---

## License

[MIT License](LICENSE) — Copyright (c) 2026 Upstate Data Systems LLC.
