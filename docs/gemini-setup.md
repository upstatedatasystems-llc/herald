# Gemini API Setup Guide

## Overview

Herald uses Google's Gemini API to transform incoming email content into structured JSON podcast scripts.

## Setup Steps

1. Obtain a Gemini API Key from [Google AI Studio](https://aistudio.google.com/).
2. Add your API key to `.env`:
   ```env
   GEMINI_API_KEY=AIzaSy...
   GEMINI_MODEL=gemini-1.5-flash
   ```
3. Test your key by running:
   ```bash
   make test
   ```
