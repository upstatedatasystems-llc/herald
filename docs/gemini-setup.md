# Gemini API Setup Guide (Historical)

> [!NOTE]
> **Historical Design Reference**: This document describes Herald's legacy Gemini-first setup.
> Herald now features a fully **vendor-neutral multi-provider architecture** supporting Google Gemini,
> Groq, Cloudflare Workers AI, OpenAI, OpenRouter, Mistral, Anthropic, Ollama, and Literal (Zero-AI).
> See [Architecture Reference](architecture.md) and [Deployment Guide](deployment.md) for current configuration.

## Overview

Herald supports Google Gemini alongside multiple external providers to transform incoming content into structured JSON podcast scripts. When configured with Gemini, Herald supports Google Search Grounding for `research` mode.

## Setup Steps

1. Obtain a Gemini API Key from [Google AI Studio](https://aistudio.google.com/).
2. Add your API key to `.env`:
   ```env
   AI_PROVIDER=gemini
   GEMINI_API_KEY=AIzaSy...
   GEMINI_MODEL=gemini-3.5-flash
   GEMINI_RESEARCH_MODEL=gemini-3.6-flash
   ```
3. Test your configuration via `/ai-check` in Telegram or run unit tests:
   ```bash
   uv run pytest tests/unit/test_phase2f_ai_providers.py
   ```

