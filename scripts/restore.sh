#!/usr/bin/env bash
set -euo pipefail

BACKUP_PATH="${1:-}"

if [ -z "${BACKUP_PATH}" ]; then
    echo "Usage: $0 <path-to-backup-dir>"
    exit 1
fi

if [ ! -d "${BACKUP_PATH}" ]; then
    echo "Error: Backup directory '${BACKUP_PATH}' does not exist."
    exit 1
fi

echo "Validating backup manifest and files in '${BACKUP_PATH}'..."

if [ ! -f "${BACKUP_PATH}/database.sql" ]; then
    echo "Error: Database backup 'database.sql' missing."
    exit 1
fi

if [ ! -s "${BACKUP_PATH}/database.sql" ]; then
    echo "Error: Database backup 'database.sql' is 0 bytes."
    exit 1
fi

echo "Backup artifact validation passed successfully."
echo "To perform live restore:"
echo "  cat ${BACKUP_PATH}/database.sql | docker exec -i herald-postgres psql -U herald herald"
echo "  alembic upgrade head"
