# Backup & Restore Procedures

## Database Backup

Run the automated database backup script:

```bash
make backup
```

Backups are saved to `/opt/herald/backups/herald_db_YYYYMMDD_HHMMSS.sql.gz`.
Backups older than 30 days are pruned automatically.

## Database Restore

Restore database from a SQL backup file:

```bash
make restore BACKUP_FILE=/opt/herald/backups/herald_db_20260806_120000.sql.gz
```
