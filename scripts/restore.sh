#!/usr/bin/env bash
set -euo pipefail

BACKUP_PATH="${1:-}"

if [ -z "${BACKUP_PATH}" ]; then
    echo "Usage: $0 <path-to-backup-dir>" >&2
    exit 1
fi

if [ ! -d "${BACKUP_PATH}" ]; then
    echo "Error: Backup directory '${BACKUP_PATH}' does not exist." >&2
    exit 1
fi

echo "Validating backup manifest and files in '${BACKUP_PATH}'..."

if [ ! -f "${BACKUP_PATH}/database.sql" ]; then
    echo "Error: Database backup 'database.sql' missing." >&2
    exit 1
fi

if [ ! -s "${BACKUP_PATH}/database.sql" ]; then
    echo "Error: Database backup 'database.sql' is 0 bytes." >&2
    exit 1
fi

if [ -f "${BACKUP_PATH}/checksums.txt" ]; then
    echo "Verifying SHA-256 checksums..."
    (cd "${BACKUP_PATH}" && (sha256sum -c checksums.txt >/dev/null 2>&1 || shasum -a 256 -c checksums.txt >/dev/null 2>&1))
    echo "Checksum verification passed."
fi

echo "Backup artifact validation passed successfully."
