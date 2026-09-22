# Kokoro TTS and Voice Setup

Herald uses Kokoro-FastAPI for local speech synthesis.

The default Docker Compose service is:

```text
ghcr.io/remsky/kokoro-fastapi-cpu:v0.7.1
```

Kokoro runs on the internal Herald Docker network and is not published as a public service by default.

## Health check

The Compose health check verifies:

```text
http://localhost:8880/v1/models
```

From the Herald project directory:

```bash
docker compose ps
docker compose logs --tail=100 kokoro
```

For an end-to-end audio smoke test:

```bash
make smoke
```

## Voice catalog

Herald maintains a curated voice catalog and can also discover compatible voices exposed by the running Kokoro service.

The default allowlist currently includes American and British English voices such as:

- Heart, Bella, Nicole, Sarah
- Adam, Michael, Fenrir, Puck
- Emma, Isabella
- Fable, George

Use Telegram to browse the active catalog:

```text
/voices
/settings
```

The active set is controlled by `ALLOWED_VOICES` and can be intersected with runtime discovery when:

```env
HERALD_VOICE_DISCOVERY_ENABLED=true
```

## Voice previews

Preview MP3s are cached in the shared Herald work volume under:

```text
/data/herald/voice_samples/
```

The cache contains both the preview audio files and a versioned `manifest.json`.

To build or repair missing preview samples, run this while normal podcast TTS is idle:

```bash
docker compose exec -T telegram-bot   python -m herald.services.voice_manager --prewarm
```

To force regeneration:

```bash
docker compose exec -T telegram-bot   python -m herald.services.voice_manager --prewarm --force
```

Podcast synthesis has priority over preview generation. If the shared TTS slot is busy, preview generation may defer or fail temporarily.

## Voice and speed configuration

Server defaults are controlled by:

```env
KOKORO_VOICE="af_heart"
KOKORO_SPEED=1.0
ALLOWED_VOICES="..."
```

A paired user can change the default voice and speed through `/settings`. Individual requests can also use top-of-message directives:

```text
Voice: bm_george
Speed: 1.0
```

The accepted speed range is enforced by Herald configuration.

## Audio pipeline

Herald does not simply concatenate raw TTS responses. The worker also handles:

- spoken-text normalization;
- pronunciation lexicon handling;
- semantic chunk boundaries;
- deterministic pause insertion;
- chunk validation;
- final FFmpeg assembly and spoken-word normalization;
- MP3 delivery through Telegram.

Kokoro is therefore the speech engine inside a larger narration pipeline rather than the complete podcast pipeline itself.
