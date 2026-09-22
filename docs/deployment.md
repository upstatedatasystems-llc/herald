# Ubuntu 24.04 Deployment Guide

Herald is designed for self-hosted Ubuntu 24.04 LTS systems on AMD64 or ARM64. The default deployment uses Docker Compose and requires outbound network access for Telegram plus any configured external AI providers.

A normal Herald deployment does **not** require inbound webhooks, a public IP, a domain name, or an HTTPS certificate.

## Quick install

On a clean Ubuntu 24.04 host, run as a normal user with sudo privileges:

```bash
curl -fsSL https://raw.githubusercontent.com/upstatedatasystems-llc/herald/main/install.sh | bash
```

The installer targets the current `main` branch by default.

## What the installer configures

The bootstrap process:

1. verifies Ubuntu 24.04 and supported CPU architecture;
2. checks available disk space;
3. installs Git, curl, Python, Docker Engine, and Docker Compose v2 when missing;
4. creates or updates the Herald installation directory;
5. runs the configuration wizard;
6. starts PostgreSQL, Kokoro, the migration container, the Herald worker, and the Telegram bot;
7. applies Alembic migrations;
8. runs `scripts/install_acceptance.sh`; and
9. prints a one-time owner pairing code.

## Telegram bot setup

Before running Herald, create a bot with Telegram's `@BotFather` and keep the bot token available for the setup wizard.

After installation, open the bot and send the pairing command shown by the installer:

```text
/pair 123456
```

The paired Telegram account becomes the owner of that Herald installation.

## AI provider choices

The setup wizard supports:

1. Literal / no external AI
2. Google Gemini
3. Groq
4. OpenRouter
5. Mistral
6. Cloudflare Workers AI
7. OpenAI
8. Anthropic
9. Ollama

If an AI provider is selected, the wizard prompts for the required credentials or endpoint. Secondary and Tertiary providers can also be configured for deterministic failover.

Literal can be Primary but cannot be used as Secondary or Tertiary failover.

See [ai-providers.md](ai-providers.md) for the provider environment variables and failover model.

## Manual installation

If you prefer to clone first:

```bash
git clone https://github.com/upstatedatasystems-llc/herald.git ~/herald
cd ~/herald
./setup.sh
./scripts/install_acceptance.sh
```

## Installer options

Examples:

```bash
# Install to a custom path
curl -fsSL https://raw.githubusercontent.com/upstatedatasystems-llc/herald/main/install.sh   | bash -s -- --install-dir /home/ubuntu/custom-herald

# Install a specific branch or tag for development/testing
./install.sh --ref <branch-or-tag>

# Update an existing install to the configured ref
./install.sh --update

# Reinstall containers without intentionally clearing DB/configuration
./install.sh --reinstall

# Non-interactive mode requires a valid pre-existing .env
./install.sh --non-interactive
```

For production use, stay on `main` unless you intentionally want to test another ref.

## Current Docker Compose stack

```text
postgres
herald-migration
herald-worker
telegram-bot
kokoro
```

The application services share the Herald work volume. PostgreSQL stores persistent state. Kokoro provides local TTS.

Telegram uses outbound long polling. PostgreSQL and Kokoro are not published publicly by the default Compose file.

## Configuration

The setup wizard writes `.env`. Keep it private and do not commit it.

Useful baseline settings include:

```env
TELEGRAM_BOT_TOKEN="..."
AI_PROVIDER="gemini"
AI_SECONDARY_PROVIDER=""
AI_TERTIARY_PROVIDER=""

DEFAULT_CONTENT_MODE="source"
DEFAULT_TARGET_MINUTES="auto"
DEFAULT_RESEARCH_DEPTH="medium"

KOKORO_VOICE="af_heart"
KOKORO_SPEED=1.0
```

Use [`.env.example`](../.env.example) as the source of truth for supported environment variables.

## Validate the installation

Run:

```bash
./scripts/install_acceptance.sh
```

The acceptance script checks items such as:

- `.env` permissions and placeholder-secret safety;
- PostgreSQL health;
- Kokoro readiness;
- worker and Telegram service state;
- successful migrations and Alembic head parity;
- provider-chain configuration;
- disk headroom;
- persistent log paths; and
- voice preview cache/manifest expectations.

Also inspect:

```bash
docker compose ps
python3 scripts/status.py
```

## Updating

From the installation directory:

```bash
./install.sh --update
```

The update path is designed to preserve application state while pulling code, rebuilding services, applying migrations, and rerunning acceptance validation.

## Resetting a test installation

Warm reset:

```bash
./scripts/reset-herald.sh --warm
```

Cold reset:

```bash
./scripts/reset-herald.sh --cold
```

Remove configuration as well:

```bash
./scripts/reset-herald.sh --cold --remove-env
```

These commands are destructive to application state. Back up first if the installation contains jobs or preferences you need to retain.

See [operations.md](operations.md) and [backup-restore.md](backup-restore.md) for operational details.
