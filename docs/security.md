# Herald Security Reference

Herald is self-hosted, but it is still connected to Telegram and, in AI-assisted modes, to whichever AI services the operator configures. This document describes the main trust boundaries in the current Telegram-first product.

## Owner pairing and Telegram authorization

Each installation is intended to be bound to an authorized Telegram owner.

The installer generates a one-time pairing code. The owner completes pairing with:

```text
/pair <code>
```

After pairing, owner-only actions such as log export are checked against the stored owner identity.

The Telegram bot uses outbound long polling. Herald does not require a public inbound Telegram webhook.

## Credentials and secrets

- Do not commit `.env`.
- Keep `.env` restricted to the installation account; the setup/acceptance tooling expects strict permissions.
- AI API keys, the Telegram bot token, database credentials, and other secrets stay in server configuration.
- Provider-chain job snapshots contain provider/model identifiers, not API credentials.
- Application logging and diagnostic export apply credential and Authorization-header redaction.

If a secret is accidentally exposed, rotate it at the provider; redaction is a defense-in-depth measure, not a substitute for credential hygiene.

## External data flows

The mode selected by the user determines which external systems can see submitted content.

- **Telegram** carries user messages, bot controls, and delivered podcast files.
- **Source, Expanded, and Topic** can send source/research/prompt material to configured AI providers as required by the pipeline.
- **Expanded and Topic** can perform external research.
- **Literal** makes no LLM API calls.
- **Kokoro TTS and FFmpeg** run locally in the default deployment.

Literal therefore removes external LLM processing, but it does not remove Telegram from the interface/delivery path.

## URL extraction and SSRF defense

Public URL intake is treated as untrusted.

The URL extraction layer restricts schemes, resolves and inspects destinations, blocks unsafe address ranges, revalidates redirects, and applies response/time limits.

Blocked classes include loopback, private, link-local, and cloud metadata targets. This protects a self-hosted Herald instance from being used as a proxy to internal services.

Do not bypass the shared extraction layer for user-submitted URLs.

## Prompt injection boundary

Submitted text and fetched pages are untrusted data.

AI prompts separate source material from Herald's system instructions and tell the model to treat instructions found inside the source as quoted content rather than commands. Structured outputs are validated before entering later pipeline stages.

This boundary reduces prompt-injection risk, but external AI providers still receive the content necessary for the selected AI-assisted mode.

## Provider-chain safety

Herald's failover chain is explicit and bounded:

- at most Primary, Secondary, and Tertiary candidates;
- only configured providers can be selected;
- duplicate candidates are rejected;
- Literal cannot be Secondary or Tertiary;
- execution does not escape the job's snapshotted chain.

This avoids silently routing user content to an arbitrary provider that was not configured for the job.

## Container and network boundaries

The default stack keeps PostgreSQL and Kokoro on the Docker network and does not publish them as public services.

Only outbound connectivity needed for Telegram, AI providers, research sources, and normal package/update operations should be allowed.

Host administration should use normal secure server practices such as SSH with key authentication, firewalling, patching, and restricted sudo access.

## Logs and diagnostics

Herald maintains bounded logs under `./logs/` and terminal diagnostic bundles under `./logs/diagnostics/`.

Diagnostic data is intended to preserve:

- job IDs and state transitions;
- timing and retry information;
- provider/model identifiers;
- error categories;
- bounded execution metadata.

It should not intentionally preserve plaintext API keys, Telegram tokens, Authorization headers, or other configured secrets.

The `/logs` command is owner-only because even sanitized operational logs can contain sensitive context.

## Local audio and retained files

Work files and voice preview samples live in the shared Herald work volume. Oversized Telegram audio may be retained locally for administrator recovery when delivery cannot proceed.

Treat the work volume and host filesystem as private application data.

## Reporting a security issue

Do not publish secrets or exploit details in a public issue. Use the project contact information on https://upstatedatasystems.com/Herald for private coordination when appropriate.
