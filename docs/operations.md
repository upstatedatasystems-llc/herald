# Operations & Operational Runbook

## Status & Monitoring

Check current system status, database queue depth, active job, disk space, and dependency health:

```bash
make status
```

Output example:

```text
==================================================
       HERALD AUTOMATION SYSTEM STATUS            
==================================================
Environment:       production
Work Directory:    /data/herald
Allowed Senders:   authorized_user@example.com

--- Database Queue Status ---
Total Jobs:        12
Queued Jobs:       0
Active Jobs:       0
Completed Jobs:    12
Failed Jobs:       0

--- Service & Dependency Health ---
FFmpeg Installed:  YES
Kokoro API Ready:  YES
Kokoro Models:     YES
```

## Reviewing Container Logs

Tail logs for all services:

```bash
make logs
```

Tail logs for a specific service:

```bash
docker compose logs -f herald-worker
```

## Admin Retrying Failed Jobs

To retry a failed job via API:

```bash
curl -X POST http://localhost:8000/api/v1/jobs/<JOB_ID>/retry
```
