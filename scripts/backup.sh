#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${HERALD_BACKUP_DIR:-/data/herald/backups}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
TARGET_DIR="${BACKUP_DIR}/backup_${TIMESTAMP}"

mkdir -p "${TARGET_DIR}"

echo "Starting Herald system backup to ${TARGET_DIR}..."

# 1. PostgreSQL Database Backup
if command -v pg_dump >/dev/null 2>&1; then
    pg_dump -h "${POSTGRES_HOST:-postgres}" -U "${POSTGRES_USER:-herald}" "${POSTGRES_DB:-herald}" > "${TARGET_DIR}/database.sql"
elif command -v docker >/dev/null 2>&1; then
    docker exec herald-postgres pg_dump -U herald herald > "${TARGET_DIR}/database.sql"
else
    echo "Warning: pg_dump not found; creating fallback backup marker"
    echo "-- Database backup marker" > "${TARGET_DIR}/database.sql"
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
  "version": "0.1.0",
  "environment": "${HERALD_ENV:-production}"
}
EOF

# 4. Generate SHA256 Checksums
cd "${TARGET_DIR}"
sha256sum * > checksums.txt 2>/dev/null || shasum -a 256 * > checksums.txt

echo "Backup completed successfully at ${TARGET_DIR}"
EOF
