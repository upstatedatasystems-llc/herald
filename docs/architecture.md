# Herald Architectural Reference

## System Overview

Herald is an email-to-podcast automation system optimized for single-core ARM64 cloud deployments (specifically OCI VM.Standard.A1.Flex with 1 OCPU and 6 GB RAM).

The system converts emails (plain text, HTML, forwarded newsletters, or article URLs) into structured podcast scripts via Gemini AI, synthesizes spoken-word audio using a local Kokoro TTS engine on ARM64 CPU, normalizes and encodes mono MP3 audio via FFmpeg, uploads episodes to Google Drive, and delivers email notifications to authorized users.

## Logical Architecture & Service Boundaries

```text
[ Gmail Inbox ]
       │ (Polling trigger)
       ▼
    [ n8n ] ──────► [ Herald API ] ──────► [ PostgreSQL ]
                         │                     ▲
                         ▼                     │
                  [ Gemini API ]               │ (Claim job)
                                               │
                                       [ Herald Worker ]
                                               │
                                               ▼
                                      [ Kokoro TTS API ]
                                               │
                                               ▼
                                      [ FFmpeg Builder ]
```

## Component Responsibilities

1. **PostgreSQL 16**: System of record for incoming messages, normalized source content, durable job queue, state transitions, generated scripts, audio metadata, Google Drive links, and error history.
2. **n8n Orchestrator**: Handles Gmail polling intake, allowlist checks, calling Herald API endpoints, uploading finished MP3s to Google Drive, sending completion emails, and retrying failed stages.
3. **Herald API (FastAPI)**: Handles intake validation, email text parsing, SSRF-protected article extraction, Gemini API scripting requests, script schema validation, job status queries, and delivery metadata updates.
4. **Herald Worker**: Daemon process claiming one `QUEUED` job at a time using `SELECT ... FOR UPDATE SKIP LOCKED`, chunking text on sentence boundaries, calling Kokoro TTS per segment with crash recovery, and assembling output MP3s via FFmpeg.
5. **Kokoro TTS (Kokoro-FastAPI)**: Containerized local ONNX speech synthesis service running on CPU ARM64. Exposes an OpenAI-compatible `/v1/audio/speech` endpoint over the private internal Docker network.
6. **FFmpeg Audio Pipeline**: Concatenates audio segments, applies integrated spoken-word loudness normalization (`loudnorm` filter), encodes 64k mono MP3, embeds ID3 tags, and computes SHA-256 checksums.

## Network Security & Isolation

- **Private Docker Network**: PostgreSQL, Herald Worker, Herald API, and Kokoro TTS communicate over an internal bridge network (`herald-backend`).
- **No Public Ports**: Neither PostgreSQL nor Kokoro ports are published to the public host interface.
- **n8n Editor Access**: n8n editor port (`5678`) and Herald API port (`8000`) are bound strictly to `127.0.0.1` and accessed via SSH tunneling or Tailscale.
