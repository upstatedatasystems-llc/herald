#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="/opt/herald/backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
DB_BACKUP_FILE="${BACKUP_DIR}/herald_db_${TIMESTAMP}.sql.gz"

echo "=== Herald Backup Starting: ${TIMESTAMP} ==="

mkdir -p "${BACKUP_DIR}"

# Backup PostgreSQL Database
if command -v docker &> /dev/null && docker ps | grep -q herald-postgres; then
    echo "Creating PostgreSQL dump from container herald-postgres..."
    docker exec herald-postgres pg_dump -U "${POSTGRES_USER:-herald}" "${POSTGRES_DB:-herald}" | gzip > "${DB_BACKUP_FILE}"
    echo "Database backup saved to: ${DB_BACKUP_FILE}"
else
    echo "Warning: Container 'herald-postgres' is not running."
fi

# Prune backups older than 30 days
find "${BACKUP_DIR}" -name "herald_db_*.sql.gz" -type f -mtime +30 -delete

echo "=== Herald Backup Complete ==="
