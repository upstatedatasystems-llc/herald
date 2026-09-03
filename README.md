# Herald — Telegram-First Podcast Automation System

[![CI Workflow](https://github.com/upstatedatasystems-llc/herald/actions/workflows/ci.yml/badge.svg)](https://github.com/upstatedatasystems-llc/herald/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Herald is an automated podcast generation system designed to turn articles, newsletters, notes, and documents into high-quality spoken audio podcasts delivered directly through Telegram.

Herald operates behind NATs and firewalls using outbound Telegram long polling without requiring open ports, public IPs, domains, HTTPS certificates, Gmail, Google Drive, Google OAuth, or even an AI API key.

---

## Key Features

- **Telegram-First Interface**: Send an article URL, pasted text, or forwarded message directly to your private Telegram bot and receive the completed MP3 podcast in response.
- **Literal Mode (AI is Optional)**: Functions 100% locally on your host with **zero** LLM API calls, performing deterministic text cleaning, heading preservation, sentence-aware chunking, and Kokoro TTS narration.
- **AI-Powered Modes (Gemini)**: When configured with a Gemini API key, access `brief`, `standard`, and `research` modes with grounded web verification and fidelity audits.
- **Secure Owner Pairing**: Prevents unauthorized access using a single-owner one-time pairing code displayed strictly in server console output / container logs (`/pair <code>`).
- **Outbound Long Polling**: No inbound ports, webhooks, or public IP addresses required.
- **Local Neural Speech Synthesis**: Powered by Kokoro-82M TTS and FFmpeg spoken-word loudness normalization (`loudnorm`).
- **Durable Job Engine**: PostgreSQL-backed state machine with automatic crash recovery, lease renewals, and transport-level idempotency.

---

## Getting Started

### 1. Create a Telegram Bot
1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow instructions to name your bot.
3. Copy the HTTP API token provided by BotFather.

### 2. Run Setup Wizard
On your Linux host, clone the repository and run the setup script:

```bash
git clone https://github.com/upstatedatasystems-llc/herald.git
cd herald
./setup.sh
```

The setup script will prompt you for:
1. **Telegram Bot Token** (Required)
2. **AI Provider** (Optional: Gemini or None / Literal only)
3. **Gemini API Key** (Optional if Gemini selected)

### 3. Start Herald
```bash
docker compose up -d
```

### 4. Pair Your Account
Look at your server startup logs (e.g. `docker compose logs telegram-bot`). The pairing code is displayed only in trusted server console logs:

```text
/pair 123456
```
*(Replace `123456` with the active pairing code shown in your console).*

---

## How to Use Herald

Once paired, simply message your Telegram bot:

### 1. Literal Reading (No AI Required)
```text
https://example.com/article
literal
```
or paste raw text:
```text
# Architecture Notes
Distributed consensus algorithms ensure state consistency across replicated nodes...
```

### 2. Standard AI Podcast
```text
https://example.com/ai-breakthrough
standard
```

### 3. Deep-Dive Research
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
| `/start` | Welcome message and pairing status |
| `/help` | Concise Telegram usage instructions |
| `/voices` | Interactive voice catalog, audio sample previews, and default voice selection |
| `/download [id]` | Download completed podcast MP3 as an audio document |
| `/diagnostics [id]` | View job diagnostics card and download support bundle ZIP |
| `/status` | Live runtime, TTS readiness, AI provider health, queue, disk, and uptime |
| `/ai_check` | Dedicated AI API configuration and connection test |
| `/queue` | View pending and in-progress podcast jobs |
| `/settings` | View current user-facing configuration and preferences |
| `/readme` | Send the project `README.md` document |
| `/pair <code>` | Pair Telegram account as instance owner |

---

## Architecture

```
           Telegram / HTTP / Email
                     ↓
         [ Transport-Neutral Core ]
     (Intake, Dedup, SSRF URL Extraction)
                     ↓
             [ Mode Dispatch ]
             /               \
   [ Literal Engine ]    [ AIProvider ]
   (Local/Deterministic) (Gemini Scripting & Research)
             \               /
          [ PodcastJob Queue ]
                     ↓
         [ Kokoro TTS Synthesis ]
        (Concurrency-Controlled Chunk Synthesis)
                     ↓
        [ FFmpeg Audio Assembly ]
                     ↓
        [ Transport Delivery ]
     (Direct Telegram MP3 Delivery)
```

---

## Operational Commands

Herald includes a standard `Makefile` for operations and testing:

| Command | Description |
| :--- | :--- |
| `make up` | Start all Herald services |
| `make down` | Stop all services |
| `make logs` | Tail service container logs |
| `make test` | Run full pytest suite |
| `make status` | Display queue depth and system status |

---

## License

[MIT License](LICENSE) — Copyright (c) 2026 Upstate Data Systems LLC.
