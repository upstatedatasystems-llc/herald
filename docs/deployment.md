# Ubuntu 24.04 Deployment Guide (AMD64 & ARM64)

This guide provides instructions for deploying Herald on Ubuntu 24.04 LTS servers (including Oracle Cloud Infrastructure OCI Ampere A1 `VM.Standard.A1.Flex`, AWS EC2, GCP Compute Engine, or bare metal).

---

## 1. Quick Bootstrap Installation

On a clean Ubuntu 24.04 LTS host, execute the single-line bootstrap installer:

```bash
curl -fsSL https://raw.githubusercontent.com/upstatedatasystems-llc/herald/main/install.sh | bash
```

### Advanced Installer Flags

The installer supports several arguments for testing and customization:

```bash
# Install to custom directory
install.sh --install-dir /srv/herald

# Install a specific Git branch or tag
install.sh --ref feature/telegram-phase2-productization

# Update an existing installation in-place
install.sh --update

# Reinstall existing installation without resetting database/configuration
install.sh --reinstall

# Run non-interactively (requires pre-existing valid .env)
install.sh --non-interactive
```

---

## 2. What the Bootstrap Installer Does

1. **System & Architecture Guards**: Confirms the host is running Ubuntu 24.04 LTS on `amd64` or `arm64`.
2. **Disk Space Check**: Confirms at least 4 GB free disk space (fails `<4 GB`, warns `<8 GB`).
3. **Prerequisites**: Installs `git`, `curl`, Docker Engine, and the Docker Compose plugin via official Docker APT repositories if missing.
4. **Permissions Handoff**: Safely configures user group membership for Docker without requiring manual re-login.
5. **Configuration Wizard**: Prompts for your Telegram Bot Token and chosen AI provider (Gemini, Groq, OpenRouter, Mistral, Cloudflare, or None/Literal). Writes `.env` with strict `0600` permissions.
6. **Stack Launch & Migrations**: Launches core services (`postgres`, `kokoro`, `herald-worker`, `telegram-bot`) and executes Alembic schema migrations (`herald-migration`).
7. **Acceptance Testing**: Automatically executes `scripts/install_acceptance.sh` to guarantee database schema matches latest Alembic head, default services are healthy, and optional legacy services remain isolated.
8. **Pairing Output**: Displays your instance owner pairing code.

---

## 3. Manual Step-by-Step Installation (Alternative)

If you prefer to clone and configure manually:

```bash
# 1. Clone repository
git clone https://github.com/upstatedatasystems-llc/herald.git ~/herald
cd ~/herald

# 2. Run setup wizard
./setup.sh

# 3. Verify acceptance
./scripts/install_acceptance.sh
```

---

## 4. Upgrading Herald

To pull the latest code and apply schema migrations:

```bash
cd ~/herald
./install.sh --update
```

This will:
- Verify clean working tree.
- Fast-forward pull the latest release.
- Rebuild container images.
- Run Alembic schema migrations.
- Restart services.
- Execute acceptance validation.

---

## 5. System Reset & Reinstallation Testing

Herald provides `scripts/reset-herald.sh` to safely manage runtime lifecycle testing:

### Warm Reset (Reset Application State)
Destroys database jobs, pairing state, and work-volume audio while preserving `.env` and built container images:
```bash
./scripts/reset-herald.sh --warm
```

### Cold Reset (Clean Build Environment)
Destroys database jobs, work volumes, and removes locally built Herald container images (preserves `.env` and upstream images like `postgres` and `kokoro`):
```bash
./scripts/reset-herald.sh --cold
```

### Full Reset (Including Configuration)
To also remove the `.env` file containing credentials:
```bash
./scripts/reset-herald.sh --cold --remove-env
```

---

## 6. Acceptance Validation

Run the non-secret acceptance verification tool at any time:

```bash
./scripts/install_acceptance.sh
```

Checks:
- `.env` exists with strict `0600` permissions.
- No default placeholder passwords or tokens.
- PostgreSQL is healthy.
- Kokoro TTS engine is healthy (`/v1/models` ready).
- Herald Worker and Telegram Bot daemons are active.
- Migration container completed with exit code 0.
- Database schema matches live Alembic head revision.
- Optional legacy profiles (`n8n`, `herald-api`) are disabled by default.
- Host has sufficient disk headroom.
