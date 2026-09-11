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

## 5. AI Provider Chain Failover & Diagnostics

**Symptom**: Job succeeds but used a Secondary or Tertiary provider instead of Primary.
**Cause**: Primary provider encountered a non-retryable error (e.g. invalid API key, model deprecated) or exhausted retries on rate limits (429).
**Resolution**: Run `/ai-check` in Telegram to inspect provider connectivity and verify your API keys. Review the diagnostics card on the failure/fallback message in Telegram for detailed HTTP status codes and error reasons.

## 6. AI Rate Limit (HTTP 429) & Backoff

**Symptom**: Generation pauses or job logs show `AIRateLimitedError`.
**Cause**: Upstream AI provider RPM/TPM quota exceeded.
**Resolution**: Herald automatically obeys `Retry-After` response headers up to 3 bounded attempts. If your workload regularly exceeds provider rate limits, configure a Secondary provider in `/settings` (e.g. Groq as Secondary for Gemini) to ensure uninterrupted delivery.

## 7. Model Context Exceeded (HTTP 413) & Adaptation

**Symptom**: Very long articles fail or take longer to script.
**Cause**: Article source exceeds the AI model's context window.
**Resolution**: Herald automatically triggers the Large-Source Adaptation Engine to semantically chunk and distill facts into a consolidated dossier. You can increase `ADAPTATION_MAX_CHUNKS` or `ADAPTATION_MAX_DEPTH` in `.env`, or select a model with a larger context window (e.g. Gemini 3.5 Flash with 1M tokens or Llama 3.3 70B with 128k tokens).

## 8. Zero-AI Fallback (Literal Mode)

**Symptom**: AI providers are down or keys are expired.
**Cause**: Upstream outage or missing billing/credentials.
**Resolution**: Use Literal mode by sending `literal` at the top of your message or selecting **Literal (Zero AI)** as Primary in `/settings`. Literal mode performs 100% local text parsing and voice synthesis with zero external dependencies.

