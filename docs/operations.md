# Operations & Operational Runbook

This guide covers maintenance, diagnostics, backups, acceptance testing, and safe reset procedures for Herald.

---

## 1. System Health & Acceptance

### Installation Acceptance Validation
Verify the entire runtime stack without exposing credentials:

```bash
./scripts/install_acceptance.sh
```

Returns `0` when:
- `.env` exists with strict `0600` permissions.
- Required credentials are non-empty and not default placeholders.
- `postgres` and `kokoro` containers are healthy.
- `herald-worker` and `telegram-bot` daemons are running.
- `herald-migration` exited successfully with code 0.
- PostgreSQL database revision authoritatively matches the dynamic Alembic head revision.
- Optional legacy profiles (`n8n`, `herald-api`) are inactive.

### Status & Queue Monitoring
Inspect queue depth and job counts:

```bash
python3 scripts/status.py
```
or via Docker Compose:
```bash
docker compose ps
```

---

## 2. Reviewing Container Logs

Tail logs for all services:
```bash
docker compose logs -f --tail=100
```

Tail logs for a specific service:
```bash
# Telegram Bot daemon
docker compose logs -f telegram-bot

# Worker & TTS synthesis pipeline
docker compose logs -f herald-worker

# Kokoro TTS engine
docker compose logs -f kokoro

# PostgreSQL database
docker compose logs -f postgres
```

---

## 3. Safe Backups and Restores

### Backup System State
Creates a timestamped snapshot of the PostgreSQL database and version manifest:

```bash
./scripts/backup.sh
```
Snapshots are saved to `./backups/backup_<YYYYMMDD_HHMMSS>/` with SHA256 checksums.

### Restore System State
Restore a database backup into a running or freshly initialized stack:

```bash
./scripts/restore.sh ./backups/backup_20260904_120000
```

---

## 4. Safe System Reset & Reinstall Tooling

Herald includes `scripts/reset-herald.sh` to safely manage application state and testing environments.

> [!WARNING]
> Resetting destroys PostgreSQL jobs, pairing state, user preferences, and work-volume audio artifacts.
> Always run `./scripts/backup.sh` before resetting if you need to retain data.

### Warm Reset (Application State Only)
Stops containers and removes Compose volumes (`postgres_data`, `work_data`). Preserves `.env` and built container images:

```bash
./scripts/reset-herald.sh --warm
```

### Cold Reset (Clean Build State)
Stops containers, removes Compose volumes, and removes locally built Herald container images (`herald-worker`, `herald-migration`, `telegram-bot`, `herald-api`). Preserves `.env` and upstream images (`postgres:16-alpine`, `ghcr.io/remsky/kokoro-fastapi-cpu:v0.7.1`):

```bash
./scripts/reset-herald.sh --cold
```

### Deleting Configuration (.env)
To explicitly delete `.env` during a reset (requires typed confirmation or `-y`):

```bash
./scripts/reset-herald.sh --cold --remove-env
```

---

## 5. Service Lifecycle Management

```bash
# Stop all services
docker compose down

# Start all core services
docker compose up -d postgres kokoro herald-migration herald-worker telegram-bot

# Rebuild and restart services after code changes
docker compose build && docker compose up -d
```
