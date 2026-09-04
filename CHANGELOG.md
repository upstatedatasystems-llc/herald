# Changelog

All notable changes to the **Herald** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-09-04

### Added
- **Automated Bootstrap Installer (`install.sh`)**: One-line deployment for Ubuntu 24.04 LTS (AMD64 & ARM64) with OS/arch validation, disk space headroom guards (4GB hard-fail / 8GB warning), automatic Docker Engine and Compose v2 provisioning, user group handoff, and non-interactive execution support.
- **Safe Reset Tooling (`scripts/reset-herald.sh`)**: Project-scoped `--warm` and `--cold` reset utility with authoritative local image targeting, irreversible state warnings, and `--remove-env` confirmation safety.
- **Installation Acceptance Validator (`scripts/install_acceptance.sh`)**: Non-secret deployment health verification asserting active service states, dynamic Alembic schema head parity, `.env` file permissions (0600), placeholder secret auditing, and legacy profile isolation.
- **Comprehensive Deployment Test Matrix**: Unit and integration test suites (`test_install_script.py`, `test_deployment_scripts.py`) covering all installer guards, update/reinstall modes, reset scoping, and acceptance checks.

### Changed
- **Hardened Setup Wizard (`setup.sh`)**: Direct-variable assignment prompts via dedicated input file descriptor (zero secret leakage to stdout/stderr/logs), fail-fast migration verification, and `--non-interactive` support.
- **Updated Documentation**: Streamlined `README.md`, `docs/deployment.md`, and `docs/operations.md` to lead with Telegram-first bootstrap installation, operations runbooks, and reset procedures.

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
