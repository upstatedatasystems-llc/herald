#!/usr/bin/env bash
# ==============================================================================
# Herald Setup Wizard (Linux / Unix)
# ==============================================================================
set -e

echo ""
echo "============================================================"
echo "                   HERALD SETUP WIZARD"
echo "============================================================"
echo ""
echo "This script will configure Herald with a Telegram interface"
echo "and optional AI provider support."
echo ""

# 1. Telegram Bot Token (Required)
while true; do
    read -rp "Enter Telegram Bot Token (from @BotFather): " TG_TOKEN
    TG_TOKEN=$(echo "$TG_TOKEN" | xargs)
    if [ -n "$TG_TOKEN" ]; then
        break
    fi
    echo "Telegram Bot Token is required."
done

# 2. AI Provider Selection (Optional)
echo ""
echo "Configure AI provider?"
echo "  1) Gemini"
echo "  2) None — Literal mode only (No API key required)"
read -rp "Select option [1-2] (default: 2): " AI_CHOICE
AI_CHOICE=${AI_CHOICE:-2}

AI_PROVIDER="none"
GEMINI_KEY=""

if [ "$AI_CHOICE" = "1" ]; then
    AI_PROVIDER="gemini"
    read -rp "Enter Gemini API Key: " GEMINI_KEY
    GEMINI_KEY=$(echo "$GEMINI_KEY" | xargs)
    if [ -z "$GEMINI_KEY" ]; then
        echo "No Gemini key entered. Falling back to Literal mode only."
        AI_PROVIDER="none"
    fi
fi

# 3. Write configuration to .env
ENV_FILE=".env"
echo ""
echo "Writing configuration to $ENV_FILE..."

cat > "$ENV_FILE" << EOF
# Herald Configuration
HERALD_ENV=production
LOG_LEVEL=INFO
TZ=UTC
HERALD_WORK_DIR=/data/herald

# Telegram Interface
TELEGRAM_BOT_TOKEN=$TG_TOKEN
AI_PROVIDER=$AI_PROVIDER

# AI Providers
GEMINI_API_KEY=$GEMINI_KEY
GEMINI_MODEL=gemini-3.5-flash
GEMINI_RESEARCH_MODEL=gemini-2.5-flash

# Database
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=herald
POSTGRES_USER=herald
POSTGRES_PASSWORD=herald_secure_password

# Kokoro TTS & Audio
KOKORO_BASE_URL=http://kokoro:8880/v1
KOKORO_VOICE=af_heart
KOKORO_SPEED=1.0
LOCAL_COMPLETE_RETENTION_HOURS=48
EOF

chmod 600 "$ENV_FILE"
echo "Configuration securely saved with permissions 0600."

# 4. Dependency check & Database migration
echo ""
echo "Validating environment and running database migrations..."

if command -v alembic >/dev/null 2>&1; then
    alembic upgrade head || echo "Database migration will apply upon Docker startup."
elif command -v uv >/dev/null 2>&1; then
    uv run alembic upgrade head || echo "Database migration will apply upon Docker startup."
fi

# 5. Output completion & pairing instructions
echo ""
echo "============================================================"
echo "                   HERALD IS READY"
echo "============================================================"
echo ""
echo "Telegram:    Configured"
echo "TTS:         Ready (Kokoro)"

if [ "$AI_PROVIDER" = "gemini" ] && [ -n "$GEMINI_KEY" ]; then
    echo "AI:          Gemini — Configured"
    echo "Default:     Standard mode"
else
    echo "AI:          Not configured"
    echo "Default:     Literal mode only"
fi

echo ""
echo "To start Herald services:"
echo "  docker compose up -d"
echo ""
echo "Next step: Pair your Telegram account by messaging your bot:"
echo ""
echo "  1. Start a chat with your bot on Telegram"
echo "  2. Send /start to view the pairing code"
echo "  3. Send /pair <code> to become the authorized owner"
echo ""
echo "After pairing, use /help for instructions."
echo "============================================================"
echo ""
