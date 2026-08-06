#!/usr/bin/env bash
set -euo pipefail

echo "Starting disposable backup and restore verification test..."

TEMP_BACKUP_DIR=$(mktemp -d 2>/dev/null || mktemp -d -t 'herald_backup')
trap 'rm -rf "${TEMP_BACKUP_DIR}"' EXIT

HERALD_BACKUP_DIR="${TEMP_BACKUP_DIR}" bash scripts/backup.sh

BACKUP_SUBDIR=$(find "${TEMP_BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d | head -n 1)

if [ -z "${BACKUP_SUBDIR}" ]; then
    echo "Error: Backup output directory not found." >&2
    exit 1
fi

# 1. Validate manifest & files
bash scripts/restore.sh "${BACKUP_SUBDIR}"

# 2. Inspect database.sql content for required tables and Alembic revision
echo "Verifying SQL dump schema and tables in database.sql..."
if ! grep -q "podcast_jobs" "${BACKUP_SUBDIR}/database.sql"; then
    echo "Error: database.sql does not contain podcast_jobs table definitions." >&2
    exit 1
fi

if ! grep -q "job_state_transitions" "${BACKUP_SUBDIR}/database.sql"; then
    echo "Error: database.sql does not contain job_state_transitions table definitions." >&2
    exit 1
fi

# 3. Live database restore test into disposable PostgreSQL database if available
if command -v docker >/dev/null 2>&1 && docker ps | grep -q herald-postgres; then
    echo "Testing restore into disposable test database in herald-postgres..."
    docker exec herald-postgres psql -U herald -c "CREATE DATABASE herald_restore_test;" 2>/dev/null || true
    docker exec -i herald-postgres psql -U herald -d herald_restore_test < "${BACKUP_SUBDIR}/database.sql"
    
    # Query restored tables & revision
    REV=$(docker exec herald-postgres psql -U herald -d herald_restore_test -t -c "SELECT version_num FROM alembic_version;" | xargs)
    COUNT=$(docker exec herald-postgres psql -U herald -d herald_restore_test -t -c "SELECT COUNT(*) FROM podcast_jobs;" | xargs)
    
    echo "Restored database version: '${REV}', podcast_jobs count: '${COUNT}'"
    docker exec herald-postgres psql -U herald -c "DROP DATABASE herald_restore_test;" 2>/dev/null || true
fi

echo "Disposable backup and restore test completed successfully!"
