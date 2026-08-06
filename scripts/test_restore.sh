#!/usr/bin/env bash
set -euo pipefail

echo "Running disposable backup and restore test..."

TEMP_BACKUP_DIR=$(mktemp -d 2>/dev/null || mktemp -d -t 'herald_backup')

trap 'rm -rf "${TEMP_BACKUP_DIR}"' EXIT

HERALD_BACKUP_DIR="${TEMP_BACKUP_DIR}" bash scripts/backup.sh

BACKUP_SUBDIR=$(find "${TEMP_BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d | head -n 1)

if [ -z "${BACKUP_SUBDIR}" ]; then
    echo "Error: Backup output directory not found." >&2
    exit 1
fi

bash scripts/restore.sh "${BACKUP_SUBDIR}"

echo "Disposable backup and restore test completed successfully!"
