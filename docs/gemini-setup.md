# Google Gemini Provider Setup Guide

> [!NOTE]
> Herald features a fully **vendor-neutral multi-provider architecture** supporting Google Gemini, Groq, Cloudflare Workers AI, OpenAI, OpenRouter, Mistral, Anthropic, Ollama, and Literal (Zero-AI).
> This document provides provider-specific setup details for Google Gemini.

## Overview

Herald supports Google Gemini as a primary, secondary, or tertiary AI provider for podcast scripting. Providers with search grounding capabilities (such as Google Gemini with Google Search Grounding) also power Herald's `research` mode.

## Setup Steps

1. Obtain a Gemini API Key from [Google AI Studio](https://aistudio.google.com/).
2. Add your API key and model configuration to `.env`:
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
