# Changelog

All notable changes to the **Herald** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-08-06

### Added
- Initial MVP release of Herald Email-to-Podcast Automation System.
- Core Python package (`herald`) with FastAPI API service and worker daemon.
- PostgreSQL durable queue and job state machine with Alembic migrations.
- SSRF-protected URL article extraction and email body parser.
- Gemini API integration with structured JSON output schema and prompt injection boundaries.
- Kokoro ONNX TTS integration with chunking, crash recovery, and sentence boundary support.
- FFmpeg audio normalization, mono MP3 encoding, and ID3 metadata tagger.
- n8n orchestration workflows for Gmail intake, Google Drive upload, and delivery replies.
- Docker Compose deployment files optimized for OCI Ampere A1 (1 OCPU / 6 GB RAM ARM64).
- Automated test suite (unit and integration tests) and CI GitHub Actions workflow.
- Complete operational documentation, runbooks, backup/restore procedures, and security guides.
