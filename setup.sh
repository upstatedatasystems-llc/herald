#!/usr/bin/env bash
set -euo pipefail

# Herald Telegram-First Setup Wizard
echo "========================================================"
echo "          🎙️  Herald — Telegram Setup Wizard           "
echo "========================================================"
echo ""

ENV_FILE=".env"
NON_INTERACTIVE=false

# Track whether configuration existed before setup began
ENV_EXISTED_AT_START=false
if [ -f "$ENV_FILE" ]; then
    ENV_EXISTED_AT_START=true
fi

# Parse flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        --non-interactive)
            NON_INTERACTIVE=true
            shift
            ;;
        *)
            shift
            ;;
    esac
done

# Initialize dedicated interactive input FD safely
INPUT_FD=0
if [ "${HERALD_TEST_ALLOW_STDIN:-0}" = "1" ]; then
    INPUT_FD=0
elif [ ! -t 0 ]; then
    if { exec 3< /dev/tty; } 2>/dev/null; then
        INPUT_FD=3
    else
        INPUT_FD=""
    fi
fi

prompt_value() {
    local var_name="$1"
    local prompt_msg="$2"
    local default_val="${3:-}"

    if [ "$NON_INTERACTIVE" = true ] || [ -z "$INPUT_FD" ]; then
        if [ -n "$default_val" ]; then
            printf -v "$var_name" "%s" "$default_val"
            return 0
        fi
        echo "❌ Error: Interactive input required for '${var_name}' but running non-interactively without TTY." >&2
        exit 1
    fi

    local input_tmp=""
    read -u "$INPUT_FD" -rp "$prompt_msg" input_tmp || true
    input_tmp=$(trim_str "$input_tmp")
    if [ -z "$input_tmp" ] && [ -n "$default_val" ]; then
        input_tmp="$default_val"
    fi
    printf -v "$var_name" "%s" "$input_tmp"
}

prompt_secret() {
    local var_name="$1"
    local prompt_msg="$2"

    if [ "$NON_INTERACTIVE" = true ] || [ -z "$INPUT_FD" ]; then
        echo "❌ Error: Interactive credential required for '${var_name}' but running non-interactively without TTY." >&2
        exit 1
    fi

    local input_tmp=""
    read -u "$INPUT_FD" -s -rp "$prompt_msg" input_tmp || true
    input_tmp=$(trim_str "$input_tmp")
    echo "" >&2
    printf -v "$var_name" "%s" "$input_tmp"
}

# Pure-bash whitespace trimming (zero subprocesses, no process argv leakage)
trim_str() {
    local var="$1"
    var="${var#"${var%%[![:space:]]*}"}"
    var="${var%"${var##*[![:space:]]}"}"
    printf "%s" "$var"
}

# Network validation timeouts
CURL_CONNECT_TIMEOUT="${HERALD_CURL_CONNECT_TIMEOUT:-10}"
CURL_MAX_TIME="${HERALD_CURL_MAX_TIME:-30}"
CURRENT_CURL_CFG=""

cleanup_curl_cfg() {
    if [ -n "$CURRENT_CURL_CFG" ] && [ -f "$CURRENT_CURL_CFG" ]; then
        rm -f "$CURRENT_CURL_CFG"
        CURRENT_CURL_CFG=""
    fi
}
trap cleanup_curl_cfg EXIT INT TERM

# Safe curl invocation using temporary 0600 config file (never puts secret in process argv)
call_curl_config() {
    local cfg
    cfg=$(mktemp)
    chmod 600 "$cfg"
    CURRENT_CURL_CFG="$cfg"
    cat > "$cfg"
    local res
    res=$(curl -s --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIME" -K "$cfg" || true)
    rm -f "$cfg"
    CURRENT_CURL_CFG=""
    printf "%s" "$res"
}

# Python helper to read a variable from .env safely without argv secret leakage
get_env_val() {
    local key="$1"
    if [ -f "$ENV_FILE" ]; then
        python3 -c "
import sys, os
key = sys.argv[1]
filepath = sys.argv[2]
val = ''
if os.path.exists(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith('#') and '=' in stripped:
                k, v = stripped.split('=', 1)
                if k.strip() == key:
                    val = v.strip().strip('\"').strip('\'')
sys.stdout.write(val)
" "$key" "$ENV_FILE" 2>/dev/null || true
    fi
}

# Python helper to update or append keys in .env reading secret value via stdin
set_env_val() {
    local key="$1"
    local val="$2"
    python3 -c "
import sys, os
key = sys.argv[1]
filepath = sys.argv[2]
val = sys.stdin.read().rstrip('\r\n')

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
        if k == key:
            escaped = val.replace('\\\\', '\\\\\\\\').replace('\"', '\\\"')
            new_lines.append(f'{key}=\"{escaped}\"\n')
            found = True
            continue
    new_lines.append(line)

if not found:
    escaped = val.replace('\\\\', '\\\\\\\\').replace('\"', '\\\"')
    new_lines.append(f'{key}=\"{escaped}\"\n')

with open(filepath, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
os.chmod(filepath, 0o600)
" "$key" "$ENV_FILE" <<< "$val"
}

if [ -f "$ENV_FILE" ]; then
    echo "ℹ️  Existing configuration found in ${ENV_FILE}."
fi

# 1. Telegram Bot Token
TG_TOKEN=$(get_env_val "TELEGRAM_BOT_TOKEN")

if [ -z "$TG_TOKEN" ]; then
    echo "To create a bot, message @BotFather on Telegram and send /newbot."
    while [ -z "$TG_TOKEN" ]; do
        prompt_secret TG_TOKEN "Enter your Telegram Bot Token: "
        TG_TOKEN=$(trim_str "$TG_TOKEN")
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
TG_ME_RESP=$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$TG_TOKEN" | call_curl_config)
if echo "$TG_ME_RESP" | grep -q '"ok":true'; then
    BOT_NAME=$(echo "$TG_ME_RESP" | grep -o '"username":"[^"]*' | cut -d'"' -f4 || echo "HeraldBot")
    echo "✅ Telegram Bot verified: @${BOT_NAME}"
    set_env_val "TELEGRAM_BOT_TOKEN" "$TG_TOKEN"
else
    echo "❌ Telegram Bot Token validation failed. Response: ${TG_ME_RESP}"
    echo "Please check your bot token from @BotFather and rerun setup.sh."
    exit 1
fi

# 2. AI Provider Selection & Validation
AI_PROVIDER=$(get_env_val "AI_PROVIDER")

if [ -z "$AI_PROVIDER" ]; then
    echo ""
    echo "Select an AI Scripting Provider:"
    echo "  1) None / Literal (Deterministic local reading only — zero external AI calls)"
    echo "  2) Google Gemini (Recommended — enables Brief, Standard, and Grounded Research)"
    echo "  3) Groq Cloud (Ultra-fast inference with Llama-3.3-70B)"
    echo "  4) OpenRouter (Multi-model gateway, e.g. Claude, Llama 3.3, DeepSeek)"
    echo "  5) Mistral AI (Mistral Large / Mistral Small Chat API)"
    echo "  6) Cloudflare Workers AI (Serverless Edge Inference)"
    prompt_value AI_CHOICE "Enter choice [1-6, default: 2]: " "2"

    case "$AI_CHOICE" in
        1)
            AI_PROVIDER="none"
            set_env_val "AI_PROVIDER" "none"
            set_env_val "RESEARCH_PROVIDER" "none"
            ;;
        2)
            AI_PROVIDER="gemini"
            GEMINI_KEY=""
            while [ -z "$GEMINI_KEY" ]; do
                prompt_secret GEMINI_KEY "Enter your Gemini API Key: "
                GEMINI_KEY=$(trim_str "$GEMINI_KEY")
                if [ -z "$GEMINI_KEY" ]; then
                    echo "⚠️  Gemini API Key cannot be empty when Gemini provider is selected."
                fi
            done
            set_env_val "AI_PROVIDER" "gemini"
            set_env_val "GEMINI_API_KEY" "$GEMINI_KEY"
            set_env_val "GEMINI_MODEL" "gemini-3.5-flash"
            set_env_val "RESEARCH_PROVIDER" "gemini"
            ;;
        3)
            AI_PROVIDER="groq"
            GROQ_KEY=""
            while [ -z "$GROQ_KEY" ]; do
                prompt_secret GROQ_KEY "Enter your Groq API Key (gsk_...): "
                GROQ_KEY=$(trim_str "$GROQ_KEY")
                if [ -z "$GROQ_KEY" ]; then
                    echo "⚠️  Groq API Key cannot be empty."
                fi
            done
            set_env_val "AI_PROVIDER" "groq"
            set_env_val "GROQ_API_KEY" "$GROQ_KEY"
            set_env_val "GROQ_MODEL" "llama-3.3-70b-versatile"
            ;;
        4)
            AI_PROVIDER="openrouter"
            OR_KEY=""
            while [ -z "$OR_KEY" ]; do
                prompt_secret OR_KEY "Enter your OpenRouter API Key (sk-or-...): "
                OR_KEY=$(trim_str "$OR_KEY")
                if [ -z "$OR_KEY" ]; then
                    echo "⚠️  OpenRouter API Key cannot be empty."
                fi
            done
            set_env_val "AI_PROVIDER" "openrouter"
            set_env_val "OPENROUTER_API_KEY" "$OR_KEY"
            set_env_val "OPENROUTER_MODEL" "meta-llama/llama-3.3-70b-instruct"
            ;;
        5)
            AI_PROVIDER="mistral"
            MIS_KEY=""
            while [ -z "$MIS_KEY" ]; do
                prompt_secret MIS_KEY "Enter your Mistral API Key: "
                MIS_KEY=$(trim_str "$MIS_KEY")
                if [ -z "$MIS_KEY" ]; then
                    echo "⚠️  Mistral API Key cannot be empty."
                fi
            done
            set_env_val "AI_PROVIDER" "mistral"
            set_env_val "MISTRAL_API_KEY" "$MIS_KEY"
            set_env_val "MISTRAL_MODEL" "mistral-large-latest"
            ;;
        6)
            AI_PROVIDER="cloudflare"
            CF_TOKEN=""
            CF_ACCT=""
            while [ -z "$CF_TOKEN" ]; do
                prompt_secret CF_TOKEN "Enter your Cloudflare API Token: "
                CF_TOKEN=$(trim_str "$CF_TOKEN")
            done
            while [ -z "$CF_ACCT" ]; do
                prompt_value CF_ACCT "Enter your Cloudflare Account ID: "
                CF_ACCT=$(trim_str "$CF_ACCT")
            done
            set_env_val "AI_PROVIDER" "cloudflare"
            set_env_val "CLOUDFLARE_API_TOKEN" "$CF_TOKEN"
            set_env_val "CLOUDFLARE_ACCOUNT_ID" "$CF_ACCT"
            set_env_val "CLOUDFLARE_AI_MODEL" "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
            ;;
        *)
            echo "❌ Error: Invalid AI provider choice '${AI_CHOICE}'." >&2
            exit 1
            ;;
    esac

    # Optional Gemini Research configuration for non-Gemini providers
    if [ "$AI_PROVIDER" != "gemini" ] && [ "$AI_PROVIDER" != "none" ]; then
        echo ""
        echo "ℹ️  Research mode requires Google Search Grounding (Gemini)."
        prompt_value WANT_RES "Would you like to configure an optional GEMINI_API_KEY for Research mode? [y/N]: " "N"
        if [[ "$WANT_RES" =~ ^[Yy]$ ]]; then
            prompt_secret RES_KEY "Enter Gemini API Key for Research: "
            RES_KEY=$(trim_str "$RES_KEY")
            if [ -n "$RES_KEY" ]; then
                set_env_val "GEMINI_API_KEY" "$RES_KEY"
                set_env_val "RESEARCH_PROVIDER" "gemini"
                echo "✅ Gemini Research configured alongside ${AI_PROVIDER}."
            else
                set_env_val "RESEARCH_PROVIDER" "none"
            fi
        else
            set_env_val "RESEARCH_PROVIDER" "none"
        fi
    elif [ "$AI_PROVIDER" = "none" ]; then
        set_env_val "RESEARCH_PROVIDER" "none"
    fi
else
    # Normalize aliases
    if [ "$AI_PROVIDER" = "literal" ]; then
        AI_PROVIDER="none"
        set_env_val "AI_PROVIDER" "none"
    fi

    case "$AI_PROVIDER" in
        none|gemini|groq|openrouter|mistral|cloudflare)
            echo "✅ AI Provider is configured: ${AI_PROVIDER}"
            ;;
        *)
            echo "❌ Error: Unknown AI_PROVIDER '${AI_PROVIDER}' in ${ENV_FILE}." >&2
            echo "Allowed values: none, gemini, groq, openrouter, mistral, cloudflare." >&2
            exit 1
            ;;
    esac
fi

# 3. Live Validate Active Provider Connection without Echoing Secrets
echo ""
echo "🔍 Validating AI Provider and model availability..."
AI_VALID=true
if [ "$AI_PROVIDER" = "gemini" ]; then
    G_KEY=$(get_env_val "GEMINI_API_KEY")
    G_MOD=$(get_env_val "GEMINI_MODEL")
    G_MOD=${G_MOD:-"gemini-3.5-flash"}
    if [ -z "$G_KEY" ]; then
        echo "❌ Error: Gemini API key is missing." >&2
        AI_VALID=false
    else
        GEM_RESP=$(printf 'url = "https://generativelanguage.googleapis.com/v1beta/models/%s"\nheader = "x-goog-api-key: %s"\n' "$G_MOD" "$G_KEY" | call_curl_config)
        if echo "$GEM_RESP" | grep -q '"name":'; then
            echo "✅ Gemini API connection and model '${G_MOD}' verified."
        else
            echo "⚠️  Gemini verification for '${G_MOD}' failed."
            AI_VALID=false
        fi
    fi
elif [ "$AI_PROVIDER" = "groq" ]; then
    GR_KEY=$(get_env_val "GROQ_API_KEY")
    GR_MOD=$(get_env_val "GROQ_MODEL")
    GR_MOD=${GR_MOD:-"llama-3.3-70b-versatile"}
    if [ -z "$GR_KEY" ]; then
        echo "❌ Error: Groq API key is missing." >&2
        AI_VALID=false
    else
        GR_RESP=$(printf 'url = "https://api.groq.com/openai/v1/models/%s"\nheader = "Authorization: Bearer %s"\n' "$GR_MOD" "$GR_KEY" | call_curl_config)
        if echo "$GR_RESP" | grep -q '"id":'; then
            echo "✅ Groq Cloud connection and model '${GR_MOD}' verified."
        else
            echo "⚠️  Groq verification for '${GR_MOD}' failed."
            AI_VALID=false
        fi
    fi
elif [ "$AI_PROVIDER" = "openrouter" ]; then
    OR_K=$(get_env_val "OPENROUTER_API_KEY")
    OR_MOD=$(get_env_val "OPENROUTER_MODEL")
    OR_MOD=${OR_MOD:-"meta-llama/llama-3.3-70b-instruct"}
    if [ -z "$OR_K" ]; then
        echo "❌ Error: OpenRouter API key is missing." >&2
        AI_VALID=false
    else
        OR_RESP=$(printf 'url = "https://openrouter.ai/api/v1/models"\nheader = "Authorization: Bearer %s"\n' "$OR_K" | call_curl_config)
        if echo "$OR_RESP" | grep -q "${OR_MOD}"; then
            echo "✅ OpenRouter connection and model '${OR_MOD}' verified."
        else
            echo "⚠️  OpenRouter verification for '${OR_MOD}' failed."
            AI_VALID=false
        fi
    fi
elif [ "$AI_PROVIDER" = "mistral" ]; then
    M_K=$(get_env_val "MISTRAL_API_KEY")
    M_MOD=$(get_env_val "MISTRAL_MODEL")
    M_MOD=${M_MOD:-"mistral-large-latest"}
    if [ -z "$M_K" ]; then
        echo "❌ Error: Mistral API key is missing." >&2
        AI_VALID=false
    else
        M_RESP=$(printf 'url = "https://api.mistral.ai/v1/models/%s"\nheader = "Authorization: Bearer %s"\n' "$M_MOD" "$M_K" | call_curl_config)
        if echo "$M_RESP" | grep -q '"id":'; then
            echo "✅ Mistral AI connection and model '${M_MOD}' verified."
        else
            echo "⚠️  Mistral verification for '${M_MOD}' failed."
            AI_VALID=false
        fi
    fi
elif [ "$AI_PROVIDER" = "cloudflare" ]; then
    CF_T=$(get_env_val "CLOUDFLARE_API_TOKEN")
    CF_A=$(get_env_val "CLOUDFLARE_ACCOUNT_ID")
    CF_MOD=$(get_env_val "CLOUDFLARE_AI_MODEL")
    CF_MOD=${CF_MOD:-$(get_env_val "CLOUDFLARE_MODEL")}
    CF_MOD=${CF_MOD:-"@cf/meta/llama-3.3-70b-instruct-fp8-fast"}
    if [ -z "$CF_T" ] || [ -z "$CF_A" ]; then
        echo "❌ Error: Cloudflare API Token or Account ID is missing." >&2
        AI_VALID=false
    else
        CF_RESP=$(printf 'url = "https://api.cloudflare.com/client/v4/accounts/%s/ai/models/search?search=%s"\nheader = "Authorization: Bearer %s"\n' "$CF_A" "$CF_MOD" "$CF_T" | call_curl_config)
        if echo "$CF_RESP" | grep -q '"success":true' && echo "$CF_RESP" | grep -q "${CF_MOD}"; then
            echo "✅ Cloudflare Workers AI connection and model '${CF_MOD}' verified."
        else
            echo "⚠️  Cloudflare verification for '${CF_MOD}' failed."
            AI_VALID=false
        fi
    fi
else
    echo "ℹ️  Literal mode selected (no external AI provider calls)."
fi

# Fallback safely to Literal if configured AI provider validation failed
if [ "$AI_VALID" = false ]; then
    if [ "$NON_INTERACTIVE" = true ]; then
        echo "❌ Error: Configured AI provider validation failed in non-interactive mode." >&2
        exit 1
    fi
    echo "⚠️  Configured AI provider was not verified. Falling back to Literal mode to ensure pipeline stability."
    set_env_val "AI_PROVIDER" "none"
    AI_PROVIDER="none"
fi

# Validate optional Gemini Research configuration if present
RES_PROV=$(get_env_val "RESEARCH_PROVIDER")
if [ "$RES_PROV" = "gemini" ]; then
    G_RES_K=$(get_env_val "GEMINI_API_KEY")
    G_RES_M=$(get_env_val "GEMINI_RESEARCH_MODEL")
    G_RES_M=${G_RES_M:-"gemini-2.5-flash"}
    if [ -z "$G_RES_K" ]; then
        echo "⚠️  Gemini Research validation failed (GEMINI_API_KEY missing). Disabling RESEARCH_PROVIDER."
        set_env_val "RESEARCH_PROVIDER" "none"
    else
        G_RES_RESP=$(printf 'url = "https://generativelanguage.googleapis.com/v1beta/models/%s"\nheader = "x-goog-api-key: %s"\n' "$G_RES_M" "$G_RES_K" | call_curl_config)
        if echo "$G_RES_RESP" | grep -q '"name":'; then
            echo "✅ Gemini Research model '${G_RES_M}' verified."
        else
            echo "⚠️  Gemini Research verification for '${G_RES_M}' failed. Disabling RESEARCH_PROVIDER."
            set_env_val "RESEARCH_PROVIDER" "none"
        fi
    fi
fi

# 4. Ensure internal defaults & secrets are present without overwriting existing
POSTGRES_PW=$(get_env_val "POSTGRES_PASSWORD")
if [ -z "$POSTGRES_PW" ]; then
    if [ "$ENV_EXISTED_AT_START" = true ]; then
        if [ "$NON_INTERACTIVE" = true ] || [ -z "$INPUT_FD" ]; then
            echo "❌ Error: Existing configuration in ${ENV_FILE} is missing POSTGRES_PASSWORD." >&2
            echo "Cannot regenerate password because the existing PostgreSQL volume requires the original password." >&2
            echo "Please restore POSTGRES_PASSWORD in ${ENV_FILE} or perform a reset with 'scripts/reset-herald.sh --warm'." >&2
            exit 1
        fi
        echo "⚠️  Existing configuration found, but POSTGRES_PASSWORD is missing or empty."
        echo "Do NOT generate a random password, as the existing database volume requires the original password."
        while [ -z "$POSTGRES_PW" ]; do
            prompt_secret POSTGRES_PW "Enter the existing PostgreSQL password: "
            POSTGRES_PW=$(trim_str "$POSTGRES_PW")
            if [ -z "$POSTGRES_PW" ]; then
                echo "⚠️  Password cannot be empty. Please enter the existing database password."
            fi
        done
        set_env_val "POSTGRES_PASSWORD" "$POSTGRES_PW"
    else
        POSTGRES_PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))" 2>/dev/null || openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' || true)
        if [ -z "$POSTGRES_PW" ]; then
            echo "❌ Error: Cryptographically secure random generator unavailable." >&2
            exit 1
        fi
        set_env_val "POSTGRES_PASSWORD" "$POSTGRES_PW"
    fi
fi

HERALD_API_KEY=$(get_env_val "HERALD_API_KEY")
if [ -z "$HERALD_API_KEY" ]; then
    if [ "$ENV_EXISTED_AT_START" = true ]; then
        if [ "$NON_INTERACTIVE" = true ] || [ -z "$INPUT_FD" ]; then
            echo "❌ Error: Existing configuration in ${ENV_FILE} is missing HERALD_API_KEY." >&2
            echo "Please restore HERALD_API_KEY in ${ENV_FILE}." >&2
            exit 1
        fi
        echo "⚠️  Existing configuration found, but HERALD_API_KEY is missing or empty."
        prompt_secret HERALD_API_KEY "Enter HERALD_API_KEY (leave empty to generate a new key): "
        HERALD_API_KEY=$(trim_str "$HERALD_API_KEY")
    fi
    if [ -z "$HERALD_API_KEY" ]; then
        HERALD_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null || openssl rand -base64 32 | tr -dc 'a-zA-Z0-9' || true)
        if [ -z "$HERALD_API_KEY" ]; then
            echo "❌ Error: Cryptographically secure random generator unavailable." >&2
            exit 1
        fi
    fi
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

# 5. Start Herald core services and strictly verify startup (NO false success on failure)
echo ""
echo "🚀 Starting Herald core services via Docker Compose..."
if command -v docker &> /dev/null && docker compose version &> /dev/null; then
    docker compose up -d postgres kokoro herald-migration herald-worker telegram-bot

    echo "⏳ Waiting for PostgreSQL health..."
    PG_OK=false
    for i in {1..30}; do
        PG_CID=$(docker compose ps -q postgres 2>/dev/null || true)
        if [ -n "$PG_CID" ]; then
            PG_STATUS=$(docker inspect --format='{{json .State.Health.Status}}' "$PG_CID" 2>/dev/null | tr -d '"')
            if [ "$PG_STATUS" = "healthy" ]; then
                PG_OK=true
                break
            elif [ "$PG_STATUS" = "unhealthy" ]; then
                break
            fi
        fi
        sleep 1
    done

    if [ "$PG_OK" = true ]; then
        echo "✅ PostgreSQL is healthy."
    else
        echo "❌ Error: PostgreSQL failed to become healthy. Check 'docker compose logs postgres'." >&2
        docker compose logs postgres >&2 || true
        exit 1
    fi

    echo "⏳ Waiting for database schema migrations to complete..."
    MIG_OK=false
    for i in {1..30}; do
        MIG_STATUS=$(docker compose ps -a herald-migration --format "{{.Status}}" 2>/dev/null || true)
        if echo "$MIG_STATUS" | grep -qi "Exited (0)"; then
            MIG_OK=true
            break
        elif echo "$MIG_STATUS" | grep -qEi "Exited \([1-9]"; then
            break
        fi
        sleep 1
    done

    if [ "$MIG_OK" = true ]; then
        echo "✅ Database migrations completed successfully."
    else
        echo "❌ Error: Database migration failed. Status: ${MIG_STATUS:-unknown}." >&2
        echo "Logs from herald-migration:" >&2
        docker compose logs herald-migration >&2 || true
        exit 1
    fi

    echo "⏳ Waiting for Kokoro TTS engine initialization (Docker healthcheck)..."
    KOKORO_OK=false
    for i in {1..45}; do
        K_CID=$(docker compose ps -q kokoro 2>/dev/null || true)
        if [ -n "$K_CID" ]; then
            K_STATUS=$(docker inspect --format='{{json .State.Health.Status}}' "$K_CID" 2>/dev/null | tr -d '"')
            if [ "$K_STATUS" = "healthy" ]; then
                KOKORO_OK=true
                break
            elif [ "$K_STATUS" = "unhealthy" ]; then
                break
            fi
        fi
        sleep 2
    done

    if [ "$KOKORO_OK" = true ]; then
        echo "✅ Kokoro TTS engine is healthy and ready (/v1/models)."
    else
        echo "❌ Error: Kokoro TTS health check timed out. Check 'docker compose logs kokoro'." >&2
        docker compose logs kokoro >&2 || true
        exit 1
    fi

    # Check herald-worker
    if docker compose ps --services --filter "status=running" 2>/dev/null | grep -q "^herald-worker$"; then
        echo "✅ Herald Worker daemon is running."
    else
        echo "❌ Error: Herald Worker container is not running. Check 'docker compose logs herald-worker'." >&2
        docker compose logs herald-worker >&2 || true
        exit 1
    fi

    # Check telegram-bot container
    if docker compose ps --services --filter "status=running" 2>/dev/null | grep -q "^telegram-bot$"; then
        echo "✅ Telegram Bot daemon is running."
    else
        echo "❌ Error: Telegram Bot container is not running. Check 'docker compose logs telegram-bot'." >&2
        docker compose logs telegram-bot >&2 || true
        exit 1
    fi
else
    echo "❌ Error: Docker or Docker Compose not available. Cannot start Herald services." >&2
    exit 1
fi

# 6. Retrieve active pairing status & validate setup completion gate
echo "🔍 Validating Telegram pairing state..."
RAW_PAIRING_OUTPUT=""
if ! RAW_PAIRING_OUTPUT=$(docker compose exec -T telegram-bot python -m herald.telegram.pairing_cli 2>&1); then
    echo "❌ Error: Failed to inspect Telegram pairing status from telegram-bot container." >&2
    echo "Action: Verify telegram-bot container health and database connectivity." >&2
    echo "Recent telegram-bot logs:" >&2
    docker compose logs --tail=20 telegram-bot 2>&1 | sed -E 's/(bot[0-9]+:)[A-Za-z0-9_-]+/\1[REDACTED]/g' >&2 || true
    exit 1
fi

PAIRING_OUTPUT=$(echo "$RAW_PAIRING_OUTPUT" | tr -d '\r' | awk 'NR==1{print $0}')

if [ "$PAIRING_OUTPUT" = "PAIRED" ]; then
    PAIRING_MODE="PAIRED"
elif echo "$PAIRING_OUTPUT" | grep -qE '^UNPAIRED:[A-Za-z0-9_-]+:[0-9]+$'; then
    PAIR_CODE=$(echo "$PAIRING_OUTPUT" | cut -d':' -f2)
    PAIR_EXP=$(echo "$PAIRING_OUTPUT" | cut -d':' -f3)
    if [ -z "$PAIR_CODE" ]; then
        echo "❌ Error: Pairing CLI returned an empty pairing code." >&2
        exit 1
    fi
    PAIRING_MODE="UNPAIRED"
else
    echo "❌ Error: Invalid or unexpected pairing status returned by telegram-bot: ${PAIRING_OUTPUT}" >&2
    echo "Action: Check 'docker compose logs telegram-bot' for details." >&2
    docker compose logs --tail=20 telegram-bot 2>&1 | sed -E 's/(bot[0-9]+:)[A-Za-z0-9_-]+/\1[REDACTED]/g' >&2 || true
    exit 1
fi

echo ""
echo "========================================================"
echo "               Herald Setup Complete!                  "
echo "========================================================"
echo ""
echo "Telegram Bot: @${BOT_NAME:-HeraldBot}"

if [ "$PAIRING_MODE" = "PAIRED" ]; then
    echo "Owner:        Owner already paired"
    echo ""
    echo "Your Telegram account is already paired as the authorized owner."
elif [ "$PAIRING_MODE" = "UNPAIRED" ]; then
    echo "Pairing Code: ${PAIR_CODE}"
    echo "Pairing expires in: ${PAIR_EXP:-30} minutes"
    echo ""
    echo "PAIR YOUR ACCOUNT"
    echo "1. Open a private chat with @${BOT_NAME:-HeraldBot}"
    echo "2. Send:"
    echo "   /pair ${PAIR_CODE}"
fi

echo ""
echo "QUICK START"
echo "- Send an article URL by itself for a Standard podcast."
echo "- Put \"brief\" above a URL/text for a shorter episode."
echo "- Put \"research high\" above a URL/text for deep research."
echo "- Put \"literal\" above text for zero-AI narration."
echo ""
echo "TELEGRAM COMMANDS"
echo "/start        - Quick-start guide"
echo "/help         - Full usage and directive reference"
echo "/voices       - Browse voices and preview samples"
echo "/download     - Download completed podcast MP3 document"
echo "/status       - System health, queue depth, and uptime"
echo "/ai_check     - AI provider connection test"
echo "/queue        - Pending and processing jobs"
echo "/settings     - Preferences and pre-TTS confirmation toggle"
echo "/diagnostics  - View job diagnostics and download the sanitized support bundle"
echo "/readme       - Project documentation"
echo ""
echo "SERVER COMMANDS"
echo "Live logs: docker compose logs -f --tail=100"
echo "Status:    docker compose ps"
echo "Stop:      docker compose down"
echo "Start:     docker compose up -d"
echo "========================================================"
