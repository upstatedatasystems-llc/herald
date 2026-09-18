# Herald Backlog

This backlog contains improvements identified during the September 2026 podcast-quality review that are intentionally **out of scope for the current improvement pass**.

## Deferred Improvements

### 1. Improve PDF/source-fetch reliability

The logs showed a public NASA PDF timing out during content retrieval even though DNS, TCP, and TLS connectivity succeeded.

Future work:
- Add PDF-specific retry and fallback handling.
- Treat transient HTTP/read timeouts on otherwise reachable public PDFs as retryable where appropriate.
- Avoid unnecessarily transitioning a job to final failure when alternate extraction or retry paths remain available.
- Preserve existing SSRF and source-safety protections.

### 2. Make voice discovery/cache more resilient

If Kokoro temporarily times out while Herald queries available voices, Herald should not collapse the selectable voice list to only the voices returned by a degraded probe.

Future work:
- Retain a last-known-good voice catalog with an appropriate TTL.
- Refresh the voice catalog asynchronously where practical.
- Replace the cached catalog only after a successful discovery response.
- Continue exposing previously validated voices during temporary Kokoro discovery failures.
- Preserve current voice validation and preview behavior.

### 3. Improve audio-quality observability

Some diagnostics retain configured loudness targets while actual measured integrated loudness and true-peak fields remain `null`.

Future work:
- Run a post-encode audio measurement pass.
- Record actual integrated LUFS and true-peak measurements in diagnostics.
- Keep configured targets and measured results separate.
- Make the measurements available for future regression testing and audio-quality comparisons.

## Scope Note

The items above should **not** be implemented as part of the current script-writing, TTS-normalization, pacing, status, title/intro, fidelity-gating, or token/cost improvement pass. They should remain isolated backlog items until explicitly selected for a future implementation cycle.
