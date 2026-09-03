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

# Python helper to read a variable from .env safely
get_env_val() {
    local key="$1"
    if [ -f "$ENV_FILE" ]; then
        python3 -c "
import os
val = ''
if os.path.exists('$ENV_FILE'):
    with open('$ENV_FILE', 'r', encoding='utf-8') as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith('#') and '=' in stripped:
                k, v = stripped.split('=', 1)
                if k.strip() == '$key':
                    val = v.strip().strip('\"').strip('\'')
print(val)
" 2>/dev/null || true
    fi
}

# Python helper to update or append keys in .env preserving all other lines and comments
set_env_val() {
    local key="$1"
    local val="$2"
    python3 -c "
import os
filepath = '$ENV_FILE'
lines = []
if os.path.exists(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

found = False
new_lines = []
for line in lines:
    stripped = line.strip()
    if stripped and not stripped.startswith('#') and '=' in stripped:
        k = stripped.split('=', 1)[0].strip()
        if k == '$key':
            new_lines.append(f'''$key=\"$val\"\n''')
            found = True
            continue
    new_lines.append(line)

if not found:
    new_lines.append(f'''$key=\"$val\"\n''')

with open(filepath, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
os.chmod(filepath, 0o600)
"
}

# 1. Telegram Bot Token
TG_TOKEN=$(get_env_val "TELEGRAM_BOT_TOKEN")

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
    echo "✅ Telegram Bot Token is configured."
fi

# Validate Telegram Bot Token with Bot API (fail hard on invalid token)
echo ""
echo "🔍 Validating Telegram Bot Token with api.telegram.org..."
TG_ME_RESP=$(curl -s "https://api.telegram.org/bot${TG_TOKEN}/getMe" || true)
if echo "$TG_ME_RESP" | grep -q '"ok":true'; then
    BOT_NAME=$(echo "$TG_ME_RESP" | grep -o '"username":"[^"]*' | cut -d'"' -f4 || echo "HeraldBot")
    echo "✅ Telegram Bot verified: @${BOT_NAME}"
    set_env_val "TELEGRAM_BOT_TOKEN" "$TG_TOKEN"
else
    echo "❌ Telegram Bot Token validation failed. Response: ${TG_ME_RESP}"
    echo "Please check your bot token from @BotFather and rerun setup.sh."
    exit 1
fi

# 2. AI Provider Selection & Existing Configuration Inference
AI_PROVIDER=$(get_env_val "AI_PROVIDER")
GEMINI_KEY=$(get_env_val "GEMINI_API_KEY")

if [ -z "$AI_PROVIDER" ]; then
    if [ -n "$GEMINI_KEY" ]; then
        # Preserve existing Gemini configuration on upgrade
        AI_PROVIDER="gemini"
        echo "ℹ️  Existing GEMINI_API_KEY detected. Inferred AI_PROVIDER=gemini."
        set_env_val "AI_PROVIDER" "gemini"
    else
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
            set_env_val "AI_PROVIDER" "gemini"
            set_env_val "GEMINI_API_KEY" "$GEMINI_KEY"
            set_env_val "GEMINI_MODEL" "gemini-3.5-flash"
        else
            AI_PROVIDER="none"
            set_env_val "AI_PROVIDER" "none"
        fi
    fi
else
    echo "✅ AI Provider is configured: ${AI_PROVIDER}"
fi

# 3. Test Gemini if configured
if [ "$AI_PROVIDER" = "gemini" ] && [ -n "$GEMINI_KEY" ]; then
    echo "🔍 Validating Gemini API Key..."
    GEM_RESP=$(curl -s -H "x-goog-api-key: ${GEMINI_KEY}" "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash" || true)
    if echo "$GEM_RESP" | grep -q '"name":'; then
        echo "✅ Gemini API connection verified."
    else
        echo "⚠️  Gemini verification failed. Literal mode remains fully operational."
    fi
fi

# 4. Ensure internal defaults & secrets are present without overwriting existing
POSTGRES_PW=$(get_env_val "POSTGRES_PASSWORD")
if [ -z "$POSTGRES_PW" ]; then
    POSTGRES_PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))" 2>/dev/null || openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' || echo "herald_secure_db_pass_$(date +%s)")
    set_env_val "POSTGRES_PASSWORD" "$POSTGRES_PW"
fi

HERALD_API_KEY=$(get_env_val "HERALD_API_KEY")
if [ -z "$HERALD_API_KEY" ]; then
    HERALD_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null || openssl rand -base64 32 | tr -dc 'a-zA-Z0-9' || echo "herald_internal_key_$(date +%s)")
    set_env_val "HERALD_API_KEY" "$HERALD_API_KEY"
fi

KOKORO_URL=$(get_env_val "KOKORO_BASE_URL")
if [ -z "$KOKORO_URL" ] || [ "$KOKORO_URL" = "http://kokoro:8880" ]; then
    set_env_val "KOKORO_BASE_URL" "http://kokoro:8880/v1"
fi

if [ -z "$(get_env_val "POSTGRES_DB")" ]; then set_env_val "POSTGRES_DB" "herald"; fi
if [ -z "$(get_env_val "POSTGRES_USER")" ]; then set_env_val "POSTGRES_USER" "herald"; fi
if [ -z "$(get_env_val "POSTGRES_HOST")" ]; then set_env_val "POSTGRES_HOST" "postgres"; fi
if [ -z "$(get_env_val "POSTGRES_PORT")" ]; then set_env_val "POSTGRES_PORT" "5432"; fi
if [ -z "$(get_env_val "HERALD_ENV")" ]; then set_env_val "HERALD_ENV" "production"; fi
if [ -z "$(get_env_val "HERALD_WORK_DIR")" ]; then set_env_val "HERALD_WORK_DIR" "/data/herald"; fi
if [ -z "$(get_env_val "HERALD_MIN_DISK_MB")" ]; then set_env_val "HERALD_MIN_DISK_MB" "500"; fi
if [ -z "$(get_env_val "HERALD_CONCURRENCY_PROFILE")" ]; then set_env_val "HERALD_CONCURRENCY_PROFILE" "auto"; fi
if [ -z "$(get_env_val "TELEGRAM_MAX_AUDIO_BYTES")" ]; then set_env_val "TELEGRAM_MAX_AUDIO_BYTES" "52428800"; fi
if [ -z "$(get_env_val "ALLOWED_VOICES")" ]; then set_env_val "ALLOWED_VOICES" "af_heart,af_bella,af_sarah,am_adam,am_michael"; fi
if [ -z "$(get_env_val "KOKORO_VOICE")" ]; then set_env_val "KOKORO_VOICE" "af_heart"; fi
if [ -z "$(get_env_val "KOKORO_SPEED")" ]; then set_env_val "KOKORO_SPEED" "1.0"; fi

echo "✅ Configuration file (${ENV_FILE}) is up to date (permissions: 0600)."

# 5. Start Herald core services and truthfully verify startup
echo ""
echo "🚀 Starting Herald core services via Docker Compose..."
if command -v docker &> /dev/null && docker compose version &> /dev/null; then
    docker compose up -d postgres kokoro herald-migration herald-worker telegram-bot

    echo "⏳ Waiting for PostgreSQL and schema migrations to complete..."
    MIG_OK=false
    for i in {1..30}; do
        MIG_STATUS=$(docker compose ps -a herald-migration --format "{{.Status}}" 2>/dev/null || true)
        if echo "$MIG_STATUS" | grep -qi "Exited (0)"; then
            MIG_OK=true
            break
        fi
        sleep 1
    done

    if [ "$MIG_OK" = true ]; then
        echo "✅ Database migrations completed successfully."
    else
        echo "⚠️  Migration container status: ${MIG_STATUS:-unknown}. Check 'docker compose logs herald-migration'."
    fi

    echo "⏳ Waiting for Kokoro TTS engine initialization (Docker healthcheck)..."
    KOKORO_OK=false
    for i in {1..30}; do
        K_CID=$(docker compose ps -q kokoro 2>/dev/null || true)
        if [ -n "$K_CID" ]; then
            K_STATUS=$(docker inspect --format='{{json .State.Health.Status}}' "$K_CID" 2>/dev/null | tr -d '"')
            if [ "$K_STATUS" = "healthy" ]; then
                KOKORO_OK=true
                break
            fi
        fi
        sleep 2
    done

    if [ "$KOKORO_OK" = true ]; then
        echo "✅ Kokoro TTS engine is healthy and ready (/v1/models)."
    else
        echo "⚠️  Kokoro TTS health check timed out. Model weights may still be downloading. Check 'docker compose logs kokoro'."
    fi

    # Check telegram-bot container
    if docker compose ps --services --filter "status=running" 2>/dev/null | grep -q "telegram-bot"; then
        echo "✅ Telegram Bot daemon is running."
    else
        echo "⚠️  Telegram Bot container is not running yet. Check 'docker compose logs telegram-bot'."
    fi
else
    echo "ℹ️  Docker Compose not detected. Please run 'docker compose up -d' when Docker is available."
fi

# 6. Retrieve active pairing status & display setup complete summary
PAIRING_OUTPUT=""
if command -v docker &> /dev/null && docker compose version &> /dev/null; then
    PAIRING_OUTPUT=$(docker compose exec -T telegram-bot python -m herald.telegram.pairing_cli 2>/dev/null || true)
fi

echo ""
echo "========================================================"
echo "               Herald Setup Complete!                  "
echo "========================================================"
echo ""
echo "Telegram Bot: @${BOT_NAME:-HeraldBot}"

if [ "$PAIRING_OUTPUT" = "PAIRED" ]; then
    echo "Owner:        Owner already paired"
    echo ""
    echo "Your Telegram account is already paired as the authorized owner."
elif echo "$PAIRING_OUTPUT" | grep -q "^UNPAIRED:"; then
    PAIR_CODE=$(echo "$PAIRING_OUTPUT" | cut -d':' -f2)
    PAIR_EXP=$(echo "$PAIRING_OUTPUT" | cut -d':' -f3)
    echo "Pairing Code: ${PAIR_CODE}"
    echo "Pairing expires in: ${PAIR_EXP:-30} minutes"
    echo ""
    echo "PAIR YOUR ACCOUNT"
    echo "1. Open a private chat with @${BOT_NAME:-HeraldBot}"
    echo "2. Send:"
    echo "   /pair ${PAIR_CODE}"
else
    echo "Status:       Stack running (check 'docker compose logs telegram-bot' for pairing)"
fi

echo ""
echo "QUICK START"
echo "- Send an article URL by itself for a Standard podcast."
echo "- Put \"brief\" above a URL/text for a shorter episode."
echo "- Put \"research high\" above a URL/text for deep research."
echo "- Put \"literal\" above text for zero-AI narration."
echo ""
echo "TELEGRAM COMMANDS"
echo "/start     - Quick-start guide"
echo "/help      - Full usage and directive reference"
echo "/status    - System health, queue depth, and uptime"
echo "/ai_check  - AI provider connection test"
echo "/queue     - Pending and processing jobs"
echo "/settings  - Preferences and pre-TTS confirmation toggle"
echo "/readme    - Project documentation"
echo ""
echo "SERVER COMMANDS"
echo "Live logs: docker compose logs -f --tail=100"
echo "Status:    docker compose ps"
echo "Stop:      docker compose down"
echo "Start:     docker compose up -d"
echo "========================================================"
