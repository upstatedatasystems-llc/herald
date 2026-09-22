# Backup and Restore Validation

Herald currently ships a database backup utility plus a disposable restore-verification workflow.

## Create a backup

Run:

```bash
./scripts/backup.sh
```

or:

```bash
make backup
```

By default, backups are written to:

```text
./backups/backup_YYYYMMDD_HHMMSS/
```

Each backup directory contains:

```text
database.sql
manifest.json
checksums.txt
```

`database.sql` is produced with `pg_dump`. If host `pg_dump` is unavailable, the script can use the running `herald-postgres` container.

To override the destination:

```bash
HERALD_BACKUP_DIR=/path/to/backups ./scripts/backup.sh
```

## Validate a backup artifact

`scripts/restore.sh` currently validates a backup directory; it does **not** overwrite the live Herald database.

Run:

```bash
./scripts/restore.sh ./backups/backup_YYYYMMDD_HHMMSS
```

It verifies that `database.sql` exists, is non-empty, and matches the recorded checksums when available.

## Disposable restore test

The safest built-in end-to-end verification is:

```bash
make restore-test
```

This workflow:

1. creates a fresh temporary backup;
2. validates the artifact;
3. checks that expected Herald tables and the Alembic revision are present; and
4. when the Herald PostgreSQL container is available, restores the dump into a disposable test database, queries it, and drops the test database.

It does not replace the production database.

## Production recovery

There is not currently a one-command destructive production restore in the repository.

Before any manual production restore:

1. preserve the current `.env` and a fresh backup if possible;
2. stop application writers such as `herald-worker` and `telegram-bot`;
3. restore into an empty or intentionally replaced PostgreSQL database using standard PostgreSQL tooling;
4. confirm the Alembic revision and required Herald tables;
5. restart the stack and run `./scripts/install_acceptance.sh`.

Because production restore is destructive and environment-specific, do not treat `scripts/restore.sh` as a database import command.
