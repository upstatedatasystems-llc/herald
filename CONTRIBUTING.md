# Contributing to Herald

Thank you for contributing to Herald.

Herald is an open-source, self-hosted podcast generation platform with a Telegram-first interface. Current product behavior is defined by the `main` branch and the user-facing documentation in `README.md` and `docs/`.

## Product invariants

Changes should preserve the current product model unless the change intentionally proposes a product/architecture revision:

- inputs: topic seed, public URL, pasted text, or forwarded Telegram message;
- modes: Topic, Source, Expanded, and Literal;
- Telegram as the current remote interface;
- PostgreSQL-backed durable jobs and recovery;
- provider-neutral AI with an explicit bounded failover chain;
- Literal mode with zero LLM API calls;
- local Kokoro TTS and FFmpeg audio processing;
- no required inbound Telegram webhook;
- owner pairing and secret-safe diagnostics.

If a change alters one of these assumptions, update the relevant documentation in the same pull request.

## Development setup

```bash
git clone https://github.com/upstatedatasystems-llc/herald.git
cd herald
make setup
```

Run tests:

```bash
make test
```

Useful checks also include:

```bash
python -m compileall -q herald apps migrations tests
./scripts/install_acceptance.sh
```

Run lint/format tooling as configured by the repository before submitting changes.

## Architecture guidelines

- Keep core orchestration provider-neutral. Vendor-specific behavior belongs behind provider interfaces.
- Preserve durable job state and restart-safe behavior.
- Do not silently mutate canonical source text during adaptation or research.
- Keep retries, failover, long-form adaptation, and research bounded.
- Do not bypass fidelity gates before TTS.
- Keep Telegram callback payloads within Telegram limits and validate all callback inputs.
- Keep TTS/resource usage appropriate for self-hosted systems rather than assuming large dedicated hardware.

## Security guidelines

- Never commit API keys, Telegram tokens, database passwords, OAuth tokens, or other secrets.
- User-submitted URLs must use the shared SSRF-protected extraction path.
- Treat fetched pages and submitted text as untrusted data inside AI prompts.
- Do not put provider credentials into job snapshots, diagnostics, or logs.
- Preserve redaction on operational exports and terminal diagnostic bundles.
- Do not expose PostgreSQL or Kokoro publicly by default.

## Documentation guidelines

The website and GitHub docs should describe the same product.

When user-visible behavior changes, review at least:

- `README.md`
- `docs/architecture.md`
- `docs/deployment.md`
- `docs/operations.md`
- `docs/troubleshooting.md`
- `docs/ai-providers.md` when provider behavior changes
- `.env.example` when configuration changes

Avoid documenting a feature-branch-only workflow as the normal production path.

## Pull request process

1. Branch from `main`.
2. Keep the change focused.
3. Add or update tests for behavior changes.
4. Run the relevant unit/integration/acceptance checks.
5. Update documentation when user-visible behavior or configuration changes.
6. Open a pull request targeting `main` with a clear summary and verification results.
