#!/usr/bin/env bash
set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: $0 /path/to/herald_db_YYYYMMDD_HHMMSS.sql.gz"
    exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "${BACKUP_FILE}" ]; then
    echo "Error: Backup file '${BACKUP_FILE}' does not exist."
    exit 1
fi

echo "=== Restoring Herald Database from: ${BACKUP_FILE} ==="

gunzip -c "${BACKUP_FILE}" | docker exec -i herald-postgres psql -U "${POSTGRES_USER:-herald}" -d "${POSTGRES_DB:-herald}"

echo "=== Herald Database Restore Complete ==="
