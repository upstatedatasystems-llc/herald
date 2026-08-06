# Troubleshooting & Common Issues

## 1. Unauthorized Sender Rejection

**Symptom**: Email received but no job created in database.
**Cause**: Sender email is not listed in `EMAIL_ALLOWED_SENDERS`.
**Resolution**: Add sender address to `EMAIL_ALLOWED_SENDERS` in `.env` and restart containers.

## 2. SSRF URL Extraction Blocked

**Symptom**: Job fails in state `EXTRACTING` with `SSRFVulnerabilityError`.
**Cause**: Emailed article URL resolved to loopback, private IP range, or cloud metadata IP.
**Resolution**: Verify that the URL is publicly accessible over standard HTTP/HTTPS.

## 3. Kokoro TTS Synthesis Timeout

**Symptom**: Worker job remains in state `SYNTHESIZING` or fails with `KokoroTTSError`.
**Cause**: Kokoro container is overwhelmed or missing model files on host.
**Resolution**: Run `make smoke-test` to inspect Kokoro model files and container health.

## 4. Google Drive OAuth Token Expiration

**Symptom**: Job fails in state `UPLOADING`.
**Cause**: Google OAuth refresh token expired or was revoked.
**Resolution**: Re-authenticate the Google Drive OAuth credential in n8n UI.
