# Google Gemini Provider Setup

Gemini is one of Herald's supported AI providers. Herald is not tied to Gemini; see [ai-providers.md](ai-providers.md) for the full provider list and failover model.

## Configure Gemini

Obtain an API key from Google AI Studio, then set the Gemini variables in `.env`.

The current example configuration is:

```env
AI_PROVIDER="gemini"
GEMINI_API_KEY="..."
GEMINI_MODEL="gemini-3.5-flash"
GEMINI_RESEARCH_MODEL="gemini-3.6-flash"
```

The canonical current defaults are maintained in [`.env.example`](../.env.example).

Gemini can also be configured as Secondary or Tertiary when another provider is Primary:

```env
AI_SECONDARY_PROVIDER="gemini"
```

## Validate from Telegram

Run:

```text
/ai-check
/models
/settings
```

`/ai-check` performs a fresh provider connectivity check. `/models` and `/settings` show the model/provider configuration visible to the running Herald instance.

## Research-related configuration

Herald can use a distinct Gemini research model. The relevant variables include:

```env
GEMINI_RESEARCH_MODEL="gemini-3.6-flash"
GEMINI_RESEARCH_NORMALIZATION_INITIAL_OUTPUT_TOKENS=8192
GEMINI_RESEARCH_NORMALIZATION_MAX_OUTPUT_TOKENS=16384
GEMINI_URL_CONTEXT_INITIAL_OUTPUT_TOKENS=8192
GEMINI_URL_CONTEXT_MAX_OUTPUT_TOKENS=16384
```

These are operational ceilings, not podcast-length controls. Podcast duration is selected separately through the interactive Telegram configuration card or user defaults.

## Mode behavior

- **Source** uses supplied material as the factual boundary.
- **Expanded** uses the source as an anchor and adds bounded outside context.
- **Topic** builds a research-backed episode from a topic seed.
- **Literal** bypasses Gemini entirely.

Provider and model capabilities can change over time. If a configured Gemini model becomes unavailable, select another valid model or configure a failover provider rather than assuming a hard-coded model name will remain permanent.
