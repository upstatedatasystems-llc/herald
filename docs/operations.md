# Herald Operations Runbook

This runbook covers normal health checks, logs, diagnostics, backups, voice preview maintenance, service lifecycle, and updates.

## Health and acceptance

Run the installation acceptance suite:

```bash
./scripts/install_acceptance.sh
```

Runtime status:

```bash
python3 scripts/status.py
docker compose ps
```

From Telegram, the paired owner can also use:

```text
/status
/queue
/ai-check
```

`/status` reports the running application's health view, while `/ai-check` performs fresh AI provider connectivity checks.

## Logs

Follow all container logs:

```bash
docker compose logs -f --tail=100
```

Individual services:

```bash
docker compose logs -f telegram-bot
docker compose logs -f herald-worker
docker compose logs -f kokoro
docker compose logs -f postgres
```

Herald also writes bounded host logs under:

```text
./logs/
├── telegram-bot.log
├── telegram-bot.log.1
├── herald-worker.log
├── herald-worker.log.1
└── diagnostics/
```

The application logs rotate independently of Docker's own bounded `json-file` logging.

### Owner log export from Telegram

The paired owner can request a sanitized time-bounded ZIP:

```text
/logs YYYY-MM-DD [HH:MM]
```

The configured Herald timezone is used to interpret the requested start time. The export includes matching application logs and diagnostic bundles for the requested period.

## Job diagnostics

Use:

```text
/diagnostics
/diagnostics <job-id>
```

Terminal jobs generate sanitized diagnostic archives under `./logs/diagnostics/`. These are retained according to `DIAGNOSTICS_RETENTION_DAYS`.

Use diagnostics before reaching for direct database edits. They capture state transitions, provider activity, timing, failure classification, and other execution telemetry without intentionally including configured secrets.

## Voice preview cache

Voice selection and actual podcast TTS can work even when preview samples are missing. If Telegram reports that voice preview is unavailable, rebuild the preview cache while podcast synthesis is idle:

```bash
docker compose exec -T telegram-bot   python -m herald.services.voice_manager --prewarm
```

Force regeneration if the cache or manifest is stale:

```bash
docker compose exec -T telegram-bot   python -m herald.services.voice_manager --prewarm --force
```

Preview audio is stored in the shared work volume under `/data/herald/voice_samples/`.

Podcast TTS takes priority over preview generation, so perform a bulk prewarm when no active synthesis job is running.

## Backups

Create a database backup:

```bash
./scripts/backup.sh
```

or:

```bash
make backup
```

Verify the backup/restore path using a disposable test database:

```bash
make restore-test
```

See [backup-restore.md](backup-restore.md) before attempting production recovery. The current `scripts/restore.sh` validates a backup artifact; it does not overwrite the live database.

## Service lifecycle

Stop the stack:

```bash
docker compose down
```

Start the stack:

```bash
docker compose up -d
```

Restart application services:

```bash
docker compose restart telegram-bot herald-worker
```

Rebuild after source changes:

```bash
docker compose build
docker compose up -d
```

Apply migrations explicitly:

```bash
docker compose run --rm herald-migration
```

## Updates

Use the installer-managed update path for normal upgrades:

```bash
./install.sh --update
```

After an update, verify:

```bash
./scripts/install_acceptance.sh
docker compose ps
```

Then run `/status` and `/ai-check` from Telegram.

## Reset procedures

Warm reset:

```bash
./scripts/reset-herald.sh --warm
```

Cold reset:

```bash
./scripts/reset-herald.sh --cold
```

Delete `.env` as well:

```bash
./scripts/reset-herald.sh --cold --remove-env
```

Resetting destroys PostgreSQL jobs, pairing state, preferences, and work-volume artifacts. Back up first when that state matters.

## Routine operational checks

A simple maintenance pass is:

```bash
cd ~/herald
docker compose ps
python3 scripts/status.py
./scripts/install_acceptance.sh
```

Then check Telegram:

```text
/status
/ai-check
```

Investigate any failure with `/diagnostics`, `/logs`, and the service logs before changing persistent state.
