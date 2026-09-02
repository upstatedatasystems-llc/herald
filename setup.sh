#!/usr/bin/env bash
set -euo pipefail

# Herald Telegram-First Setup Wizard
echo "========================================================"
echo "          🎙️  Herald — Telegram Setup Wizard           "
echo "========================================================"
echo ""

ENV_FILE=".env"
EXISTING_ENV=false

if [ -f "$ENV_FILE" ]; then
    EXISTING_ENV=true
    echo "ℹ️  Existing configuration found in ${ENV_FILE}."
fi

# 1. Telegram Bot Token
TG_TOKEN=""
if [ "$EXISTING_ENV" = true ]; then
    TG_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d '=' -f2- | tr -d '"' || true)
fi

if [ -z "$TG_TOKEN" ]; then
    echo "To create a bot, message @BotFather on Telegram and send /newbot."
    while [ -z "$TG_TOKEN" ]; do
        read -s -rp "Enter your Telegram Bot Token: " TG_TOKEN
        echo ""
        TG_TOKEN=$(echo "$TG_TOKEN" | xargs)
        if [ -z "$TG_TOKEN" ]; then
            echo "⚠️  Token cannot be empty. Please enter a valid token."
        fi
    done
else
    echo "✅ Telegram Bot Token is already configured."
fi

# 2. AI Provider Selection
AI_PROVIDER="none"
GEMINI_KEY=""

if [ "$EXISTING_ENV" = true ]; then
    AI_PROVIDER=$(grep -E '^AI_PROVIDER=' "$ENV_FILE" | cut -d '=' -f2- | tr -d '"' || echo "none")
    GEMINI_KEY=$(grep -E '^GEMINI_API_KEY=' "$ENV_FILE" | cut -d '=' -f2- | tr -d '"' || true)
fi

if [ "$EXISTING_ENV" = false ]; then
    echo ""
    echo "Select an AI Scripting Provider:"
    echo "  1) None (Deterministic Literal reading only — zero external AI calls)"
    echo "  2) Google Gemini (Enables Brief, Standard, and Deep-Dive Research modes)"
    read -rp "Enter choice [1 or 2, default: 1]: " AI_CHOICE
    AI_CHOICE=${AI_CHOICE:-1}

    if [ "$AI_CHOICE" = "2" ]; then
        AI_PROVIDER="gemini"
        while [ -z "$GEMINI_KEY" ]; do
            read -s -rp "Enter your Gemini API Key: " GEMINI_KEY
            echo ""
            GEMINI_KEY=$(echo "$GEMINI_KEY" | xargs)
            if [ -z "$GEMINI_KEY" ]; then
                echo "⚠️  Gemini API Key cannot be empty when Gemini provider is selected."
            fi
        done
    else
        AI_PROVIDER="none"
        GEMINI_KEY=""
    fi
fi

# 3. Generate internal random secrets if not present
POSTGRES_PW=""
HERALD_API_KEY=""

if [ "$EXISTING_ENV" = true ]; then
    POSTGRES_PW=$(grep -E '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d '=' -f2- | tr -d '"' || true)
    HERALD_API_KEY=$(grep -E '^HERALD_API_KEY=' "$ENV_FILE" | cut -d '=' -f2- | tr -d '"' || true)
fi

if [ -z "$POSTGRES_PW" ]; then
    POSTGRES_PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))" 2>/dev/null || openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' || echo "herald_secure_db_pass_$(date +%s)")
fi

if [ -z "$HERALD_API_KEY" ]; then
    HERALD_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null || openssl rand -base64 32 | tr -dc 'a-zA-Z0-9' || echo "herald_internal_key_$(date +%s)")
fi

# 4. Write/Update .env file
if [ "$EXISTING_ENV" = false ]; then
    cat <<EOF > "$ENV_FILE"
# ==============================================================================
# Herald Configuration (.env)
# ==============================================================================

# Core Telegram Interface
TELEGRAM_BOT_TOKEN="${TG_TOKEN}"
AI_PROVIDER="${AI_PROVIDER}"
GEMINI_API_KEY="${GEMINI_KEY}"
GEMINI_MODEL="gemini-3.5-flash"
TELEGRAM_MAX_AUDIO_BYTES=52428800

# Defaults
DEFAULT_MODE="literal"
DEFAULT_VOICE="af_heart"
DEFAULT_SPEED=1.0
ALLOWED_VOICES="af_heart,af_bella,af_sarah,am_adam,am_michael"

# Database & Internal
POSTGRES_DB="herald"
POSTGRES_USER="herald"
POSTGRES_PASSWORD="${POSTGRES_PW}"
POSTGRES_HOST="postgres"
POSTGRES_PORT=5432
HERALD_API_KEY="${HERALD_API_KEY}"

# Runtime
HERALD_ENV="production"
HERALD_WORK_DIR="/data/herald"
HERALD_MIN_DISK_MB=500
HERALD_CONCURRENCY_PROFILE="auto"

# Kokoro TTS (local CPU container)
KOKORO_BASE_URL="http://kokoro:8880"

# Optional Legacy Gmail & n8n settings
ENABLE_EMAIL_TRANSPORT=false
N8N_ENCRYPTION_KEY=""
GOOGLE_DRIVE_FOLDER_ID=""
EMAIL_ALLOWED_SENDERS=""
EOF
    chmod 600 "$ENV_FILE"
    echo "✅ Created ${ENV_FILE} with secure permissions (0600)."
fi

# 5. Validate Telegram Bot Token with Bot API
echo ""
echo "🔍 Validating Telegram Bot Token..."
TG_ME_RESP=$(curl -s "https://api.telegram.org/bot${TG_TOKEN}/getMe" || true)
if echo "$TG_ME_RESP" | grep -q '"ok":true'; then
    BOT_NAME=$(echo "$TG_ME_RESP" | grep -o '"username":"[^"]*' | cut -d'"' -f4 || echo "Bot")
    echo "✅ Telegram Bot validated: @${BOT_NAME}"
else
    echo "⚠️  Could not verify Telegram bot token with api.telegram.org. Please check your token."
fi

# 6. Test Gemini if configured
if [ "$AI_PROVIDER" = "gemini" ] && [ -n "$GEMINI_KEY" ]; then
    echo "🔍 Validating Gemini API Key..."
    GEM_RESP=$(curl -s -H "x-goog-api-key: ${GEMINI_KEY}" "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash" || true)
    if echo "$GEM_RESP" | grep -q '"name":'; then
        echo "✅ Gemini API connection verified."
    else
        echo "⚠️  Gemini verification failed. Literal mode remains fully operational."
    fi
fi

# 7. Start Herald services and run migrations
echo ""
echo "🚀 Starting Herald core services via Docker Compose..."
if command -v docker &> /dev/null && docker compose version &> /dev/null; then
    docker compose up -d postgres kokoro herald-migration herald-worker telegram-bot
    echo "⏳ Waiting for database and services initialization..."
    sleep 5
else
    echo "ℹ️  Docker Compose not detected. Please run 'docker compose up -d' to start services."
fi

# 8. Obtain or generate owner pairing code
echo ""
echo "========================================================"
echo "               🎉 Herald Setup Complete!               "
echo "========================================================"
echo ""
echo "Next step: Pair your Telegram account as instance owner."
echo ""
echo "1. Open Telegram and start a private chat with your bot."
echo "2. Run the pairing command displayed in your logs or run:"
echo "   docker compose logs telegram-bot"
echo "   Look for: Active owner pairing code: XXXXXX"
echo "3. Send in Telegram:"
echo "   /pair <code_from_logs>"
echo ""
echo "========================================================"
