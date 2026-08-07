# Herald n8n Workflows Documentation

This directory contains 7 version-controlled JSON workflow exports for n8n:

1. `workflows/email-intake.json`: Polling Gmail trigger (simplify OFF, attachments disabled), Herald API intake call, Gemini script generation, and immediate submission acknowledgment reply with completion ETA.
2. `workflows/completion-dispatcher.json`: Schedule trigger calling atomic `/delivery/claim`, uploading 3 canonical artifacts (`.mp3`, `_source.txt`, `_diagnostics.json`) to Google Drive, calling `/drive-complete` independently per artifact, sending formatted link-only Gmail completion reply, and calling `/delivery-complete`.
3. `workflows/error-handler.json`: Global error trigger workflow capturing execution failures and sending diagnostic alert emails.
4. `workflows/daily-cleanup.json`: Automated daily cleanup of temporary local MP3, source text, and diagnostics JSON files for `COMPLETE` jobs older than 48 hours.
5. `workflows/stale-job-recovery.json`: Hourly check detecting stuck or abandoned active worker claims.
6. `workflows/daily-health-report.json`: Daily status summary report checking system readiness and queue metrics.
7. `workflows/weekly-maintenance.json`: Weekly execution pruning and database maintenance.

## Importing Workflows into n8n

1. Open the n8n UI in your browser (`http://localhost:5678` or via Tailscale).
2. Go to **Workflows** -> **Import from File**.
3. Import each of the 7 JSON files in `n8n/workflows/`.
4. Configure required credentials:
   - **Gmail OAuth2 Credential**: Connect to your dedicated Gmail inbox account.
   - **Google Drive OAuth2 Credential**: Connect to your Google Drive account with folder write access.
   - **Postgres Credential**: Connect to the internal `postgres` container database.
5. Associate the Error Workflow:
   - Open **Workflow Settings** in `email-intake` and `completion-dispatcher`.
   - Set **Error Workflow** to `Herald - System Error Handler`.
6. Toggle all workflows to **Active**.
