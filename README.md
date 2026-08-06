# Herald — Email-to-Podcast Automation System

[![CI Workflow](https://github.com/upstatedatasystems-llc/herald/actions/workflows/ci.yml/badge.svg)](https://github.com/upstatedatasystems-llc/herald/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Herald is a cloud-hosted email-to-podcast automation system designed to run continuously on a resource-conscious ARM64 cloud server (such as Oracle Cloud Infrastructure Ampere A1 with 1 OCPU and 6 GB RAM).

When an authorized user sends or forwards an email or article link to a dedicated inbox, Herald automatically normalizes the text, generates a schema-constrained podcast narration script using Gemini AI, synthesizes single-host speech locally using Kokoro TTS, normalizes audio using FFmpeg, uploads the finished MP3 to Google Drive, and emails the user back with the episode link and optional attachment.

---

## Key Features

- **Email Intake & Parser**: Accepts plain text, HTML emails, forwarded newsletters, or a single article URL.
- **SSRF Protection**: Resolves DNS before connection and strictly blocks loopback, private, link-local, and cloud metadata IPs.
- **Durable Job Queue**: Uses PostgreSQL with strict state machine transitions (`RECEIVED` -> `VALIDATING` -> `EXTRACTING` -> `SOURCE_READY` -> `SCRIPTING` -> `SCRIPT_READY` -> `QUEUED` -> `SYNTHESIZING` -> `ENCODING` -> `AUDIO_READY` -> `UPLOADING` -> `DELIVERING` -> `COMPLETE`).
- **Resumable Worker**: Worker claims jobs exclusively (`FOR UPDATE SKIP LOCKED`), chunks scripts on sentence boundaries, tracks chunk progress, and resumes from crash state without re-generating completed audio.
- **Local ARM64 CPU TTS**: Speech synthesis powered by Kokoro-82M via ONNX Runtime without requiring GPU hardware.
- **Broadcast Audio Assembly**: Concatenates WAV chunks, applies integrated spoken-word loudness normalization (`loudnorm`), encodes 64k mono MP3, and embeds ID3 metadata.
- **Google Drive & Gmail Integration**: Managed via version-controlled n8n workflows.

---

## End-to-End Workflow

```text
1. Authorized user emails dedicated inbox (e.g. Subject: "Podcast: Standard")
2. n8n detects email & checks sender allowlist
3. Herald API cleans body text / extracts URL safely (SSRF protected)
4. Gemini API returns structured JSON podcast script
5. Job queued in PostgreSQL
6. Herald Worker claims job & generates audio via Kokoro TTS
7. FFmpeg normalizes loudness & encodes mono MP3
8. n8n uploads MP3 to Google Drive folder "Herald Episodes"
9. n8n emails sender completion reply with Drive link & MP3 attachment
```

---

## System Requirements

- **Server**: Ubuntu 24.04 LTS ARM64 (e.g., OCI `VM.Standard.A1.Flex`)
- **Capacity**: 1 OCPU, 6 GB RAM, 20+ GB storage
- **Runtime**: Docker Engine & Docker Compose
- **External APIs**: Gemini API Key, Google OAuth2 (Gmail & Google Drive)

---

## Quickstart & Installation

```bash
# 1. Clone repository
git clone https://github.com/upstatedatasystems-llc/herald.git
cd herald

# 2. Configure environment variables
cp .env.example .env
nano .env

# 3. Build containers and start services
make build
make up

# 4. Run database migrations
make migrate

# 5. Run Kokoro TTS & audio pipeline smoke test
make smoke-test
```

---

## Developer Commands

Herald includes a standard `Makefile` for developer operations:

| Command | Description |
| :--- | :--- |
| `make setup` | Install Python development dependencies |
| `make build` | Build Docker Compose images |
| `make up` | Start all Herald containers in background |
| `make down` | Stop all Herald containers |
| `make logs` | Tail service container logs |
| `make migrate` | Run Alembic database migrations |
| `make test` | Run pytest suite (unit & integration tests) |
| `make lint` | Run code linters (`ruff`) |
| `make format` | Format code (`ruff format`) |
| `make smoke-test` | Run Kokoro TTS and FFmpeg pipeline smoke test |
| `make status` | Display queue depth and system status |
| `make backup` | Create database backup in `/opt/herald/backups` |

---

## System Status & Monitoring

Check queue status, database metrics, disk space, and service health at any time:

```bash
make status
```

---

## Security Highlights

- **No Public Database or TTS Ports**: PostgreSQL, Herald API, Worker, and Kokoro run on a private internal bridge network (`herald-backend`).
- **Strict SSRF Protections**: Resolved IP checks reject `127.0.0.1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254`.
- **Prompt Injection Boundary**: Source text is strictly sandboxed inside `<SOURCE_DATA>` tags within Gemini system prompts.

For complete security documentation, see [docs/security.md](docs/security.md).

---

## MVP Limitations

- **Serial Processing**: Processes 1 audio job at a time to keep n8n and PostgreSQL responsive on 1 OCPU.
- **Voice Selection**: Default voice `af_heart` (configurable via environment setting).
- **Single-Host Script**: Single narration speaker.

---

## Documentation Index

- [Architecture Reference](docs/architecture.md)
- [OCI Ubuntu 24.04 ARM64 Deployment Guide](docs/deployment.md)
- [Gmail Setup Guide](docs/gmail-setup.md)
- [Google Drive Setup Guide](docs/google-drive-setup.md)
- [Gemini Setup Guide](docs/gemini-setup.md)
- [Kokoro TTS Setup Guide](docs/kokoro-setup.md)
- [Operations & Runbook](docs/operations.md)
- [Backup & Restore Procedures](docs/backup-restore.md)
- [Security Guide](docs/security.md)
- [Troubleshooting Guide](docs/troubleshooting.md)

---

## License

[MIT License](LICENSE) — Copyright (c) 2026 Upstate Data Systems LLC.
