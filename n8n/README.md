# Herald n8n Workflow Instructions

This directory contains version-controlled JSON workflow exports for n8n:

1. `workflows/email-intake.json`: Polling Gmail trigger, email validation, Herald API intake call, Gemini script generation.
2. `workflows/completion-dispatcher.json`: Schedule trigger polling Postgres for `AUDIO_READY` jobs, reading MP3, uploading to Google Drive, sending completion email with link and optional attachment.
3. `workflows/error-handler.json`: Error trigger capturing workflow execution failures and alerting administrators.

## Importing Workflows into n8n

1. Open the n8n UI in your browser (e.g. `http://localhost:5678` or via Tailscale).
2. Go to **Workflows** -> **Import from File**.
3. Select and import each of the three JSON files in `n8n/workflows/`.
4. Configure required credentials:
   - **Gmail OAuth2 Credential**: Connect to your dedicated Gmail inbox account (`https://developers.google.com/gmail/api/quickstart`).
   - **Google Drive OAuth2 Credential**: Connect to your Google Drive account with folder access.
   - **Postgres Credential**:
     - Host: `postgres`
     - Database: `${POSTGRES_DB}` (default `herald`)
     - User: `${POSTGRES_USER}` (default `herald`)
     - Password: `${POSTGRES_PASSWORD}`
     - Port: `5432`
5. Toggle all workflows to **Active**.
