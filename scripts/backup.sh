#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${HERALD_BACKUP_DIR:-./backups}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
TARGET_DIR="${BACKUP_DIR}/backup_${TIMESTAMP}"

mkdir -p "${TARGET_DIR}"

echo "Starting Herald system backup to ${TARGET_DIR}..."

# 1. PostgreSQL Database Backup
if command -v pg_dump >/dev/null 2>&1; then
    pg_dump -h "${POSTGRES_HOST:-localhost}" -U "${POSTGRES_USER:-herald}" "${POSTGRES_DB:-herald}" > "${TARGET_DIR}/database.sql"
elif command -v docker >/dev/null 2>&1 && docker ps | grep -q herald-postgres; then
    docker exec herald-postgres pg_dump -U "${POSTGRES_USER:-herald}" "${POSTGRES_DB:-herald}" > "${TARGET_DIR}/database.sql"
else
    echo "Error: Neither pg_dump nor running herald-postgres container available. Backup failed." >&2
    exit 1
fi

if [ ! -s "${TARGET_DIR}/database.sql" ]; then
    echo "Error: Generated database.sql is empty. Backup failed." >&2
    exit 1
fi

# 2. Workflow JSON & Manifest
if [ -d "n8n/workflows" ]; then
    cp -r n8n/workflows "${TARGET_DIR}/n8n_workflows"
fi

# 3. App Version Manifest
cat <<EOF > "${TARGET_DIR}/manifest.json"
{
  "timestamp": "${TIMESTAMP}",
  "system": "Herald Email-to-Podcast",
  "version": "1.0.0",
  "environment": "${HERALD_ENV:-production}"
}
EOF

# 4. Generate SHA256 Checksums
cd "${TARGET_DIR}"
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum database.sql manifest.json > checksums.txt
elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 database.sql manifest.json > checksums.txt
fi

echo "Backup completed successfully at ${TARGET_DIR}"
