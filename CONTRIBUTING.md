# Contributing to Herald

Thank you for your interest in contributing to Herald!

## Development Guidelines

1. **Architecture & Constraints**: Herald is designed to run on a 1-OCPU / 6 GB RAM ARM64 server (OCI Ampere A1). Keep processing serial, memory conscious, and non-blocking.
2. **Code Style**:
   - Python 3.12+ with standard formatting (`ruff` or `black` / `isort`).
   - Strict type hints and Pydantic models for validation.
   - Comprehensive docstrings and comments preserving business logic rationale.
3. **Security**:
   - Never commit API keys, OAuth tokens, model files, or secrets.
   - All external HTTP calls to URLs extracted from emails MUST pass through the SSRF protection layer in `packages/herald/extraction/url_extractor.py`.
   - Untrusted text inside prompts must be isolated inside `SOURCE_DATA` containers.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/upstatedatasystems-llc/herald.git
cd herald

# Initialize virtual environment and dependencies
make setup

# Run tests
make test

# Run code formatters and linters
make lint
make format
```

## Pull Request Process

1. Create a feature branch off `main`.
2. Ensure all unit and integration tests pass (`make test`).
3. Ensure linters pass cleanly (`make lint`).
4. Submit a Pull Request targeting `main` with a clear description of changes.
