# Herald — Email-to-Podcast Automation System

[![CI Workflow](https://github.com/upstatedatasystems-llc/herald/actions/workflows/ci.yml/badge.svg)](https://github.com/upstatedatasystems-llc/herald/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Herald is a cloud-hosted email-to-podcast automation system designed to run continuously on ARM64 cloud infrastructure (such as Oracle Cloud Infrastructure Ampere A1 or multi-core cloud instances).

When an authorized user sends or forwards an email or article link to a dedicated inbox, Herald automatically normalizes the text, generates a schema-constrained podcast narration script using Gemini AI (`gemini-3.5-flash`), sends an immediate submission acknowledgment reply with an estimated completion time, synthesizes speech locally using Kokoro TTS (`ghcr.io/remsky/kokoro-fastapi-cpu:v0.7.1`), normalizes audio using FFmpeg, uploads 2 canonical artifacts (`.mp3` and `_details.md`) to Google Drive, and emails the user back with a formatted private Drive link.

---

## Key Features

- **Email Intake & Parser**: Accepts plain text, HTML emails, forwarded newsletters, or a single article URL.
- **Submission Acknowledgment Email**: Replies immediately upon intake and script generation with episode title, requested format, estimated duration, and ETA range.
- **2 Canonical Drive Artifacts**: Uploads `<basename>.mp3` and `<basename>_details.md` independently and idempotently to the configured Google Drive folder.
- **Link-Only Delivery**: Permanently link-only audio delivery with rich HTML + plain-text fallback formatting.
- **Multicore Concurrency Engine**: Supports automatic CPU detection (`HERALD_CONCURRENCY_PROFILE=auto`) to scale parallel chunk synthesis across available CPU cores, or single-core compatibility mode (`HERALD_CONCURRENCY_PROFILE=single`).
- **SSRF Protection**: Resolves DNS before connection and strictly blocks loopback, private, link-local, and cloud metadata IPs.
- **Durable Job Queue**: Uses PostgreSQL with strict state machine transitions (`RECEIVED` -> `VALIDATING` -> `EXTRACTING` -> `SOURCE_READY` -> `SCRIPTING` -> `SCRIPT_READY` -> `QUEUED_TTS` -> `SYNTHESIZING` -> `ENCODING` -> `AUDIO_READY` -> `UPLOADING` -> `DELIVERING` -> `COMPLETE`).
- **Resumable Worker & Renewable Leases**: Worker claims jobs atomically (`FOR UPDATE SKIP LOCKED`), runs lightweight background lease heartbeats, chunks scripts on sentence boundaries, tracks chunk progress, and resumes synthesis on retry without duplicating completed audio.
- **Local ARM64 CPU TTS**: Speech synthesis powered by Kokoro-82M via ONNX Runtime without requiring GPU hardware, with bounded readiness grace period during CPU inference saturation.
- **Broadcast Audio Assembly**: Concatenates WAV chunks with global FFmpeg concurrency semaphores, applies integrated spoken-word loudness normalization (`loudnorm`), encodes 64k mono MP3, and embeds ID3 metadata.
- **Google Drive & Gmail Integration**: Managed via version-controlled n8n 1.123.69 workflows.

---

## Concurrency Configuration & Profiles

Herald features a container-aware concurrency profile system controlled via `HERALD_CONCURRENCY_PROFILE` in `.env`:

| Profile | Description |
| :--- | :--- |
| `auto` (Default) | Automatically detects host/cgroup CPU capacity and configures optimal parallel worker threads, Gemini script semaphores, Kokoro global/per-job slots, and FFmpeg semaphores. |
| `single` | Compatibility / resource-constrained mode. Strictly limits worker threads, script generation, TTS synthesis, and FFmpeg encoding to 1 slot (equivalent to classic serialized Herald). |
| `balanced` | Preserves a conservative, balanced allocation for shared host environments. |

### Fine-Grained Overrides
Individual concurrency boundaries can be overridden in `.env`:
- `HERALD_WORKER_CONCURRENCY`: Parallel episode worker threads inside the worker container.
- `HERALD_SCRIPT_CONCURRENCY`: Simultaneous Gemini script generation calls.
- `HERALD_TTS_GLOBAL_SLOTS`: Maximum simultaneous Kokoro synthesis calls process-wide across all jobs.
- `HERALD_TTS_PER_JOB`: Parallel synthesis workers allocated per episode.
- `HERALD_FFMPEG_CONCURRENCY`: Simultaneous FFmpeg assembly operations.
- `HERALD_N8N_CONCURRENCY`: n8n production concurrency limit.

---

## End-to-End Workflow

```text
1. Authorized user emails dedicated inbox (e.g. Subject: "Podcast: Standard")
2. n8n detects email & checks sender allowlist
3. Herald API cleans body text / extracts URL safely (SSRF protected)
4. Gemini API returns structured JSON podcast script
5. Herald sends submission acknowledgment reply with completion ETA range
6. Job queued in PostgreSQL
7. Herald Worker claims job & generates audio via Kokoro TTS (parallel chunks)
8. FFmpeg normalizes loudness & encodes mono MP3
9. n8n uploads MP3 and details Markdown to Google Drive
10. n8n emails sender completion reply with private Drive link & episode stats
```

---

## System Requirements

- **Server**: Ubuntu 24.04 LTS ARM64 or x86_64
- **Capacity**: 1+ CPU cores, 4+ GB RAM, 20+ GB storage
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
make smoke
```

---

## Operational Commands

Herald includes a standard `Makefile` for developer and production operations:

| Command | Description |
| :--- | :--- |
| `make build` | Build Docker Compose images |
| `make up` | Start all Herald containers in background |
| `make down` | Stop all Herald containers |
| `make restart` | Restart services |
| `make logs` | Tail service container logs |
| `make ps` | Display container process status |
| `make migrate` | Run Alembic database migrations |
| `make test` | Run full pytest suite (unit & integration tests) |
| `make test-postgres` | Run real PostgreSQL concurrency integration tests |
| `make readiness` | Check API readiness endpoint |
| `make smoke` | Run Kokoro TTS and FFmpeg pipeline smoke test |
| `make status` | Display queue depth and system status |
| `make backup` | Create database backup in `/opt/herald/backups` |
| `make restore-test` | Execute disposable backup restore test |

---

## Security Highlights

- **No Public Database or TTS Ports**: PostgreSQL, Herald API, Worker, and Kokoro run on a private internal bridge network (`herald-backend`).
- **Strict SSRF Protections**: Resolved IP checks reject `127.0.0.1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254`.
- **Prompt Injection Boundary**: Source text is strictly sandboxed inside `<SOURCE_DATA>` tags within Gemini system prompts.

For complete security documentation, see [docs/security.md](docs/security.md).

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
