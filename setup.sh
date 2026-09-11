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

CONFIGURE_ONLY=false
START_ONLY=false
NO_BANNER=false

# Parse flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        --non-interactive)
            NON_INTERACTIVE=true
            shift
            ;;
        --configure-only)
            CONFIGURE_ONLY=true
            shift
            ;;
        --start-only)
            START_ONLY=true
            shift
            ;;
        --no-banner)
            NO_BANNER=true
            shift
            ;;
        *)
            shift
            ;;
    esac
done

if [ "$CONFIGURE_ONLY" = true ] && [ "$START_ONLY" = true ]; then
    echo "❌ Error: Cannot specify both --configure-only and --start-only." >&2
    exit 1
fi


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

check_unsafe_db_volume() {
    if [ "$ENV_EXISTED_AT_START" = false ]; then
        if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
            local proj_name="${COMPOSE_PROJECT_NAME:-$(basename "$PWD" 2>/dev/null || echo "herald")}"
            local proj_clean
            proj_clean=$(echo "$proj_name" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]//g')
            local existing_vols
            existing_vols=$(docker volume ls -q 2>/dev/null || true)
            if echo "$existing_vols" | grep -qE "^(${proj_clean}_postgres_data|${proj_name}_postgres_data|herald_postgres_data)$"; then
                if [ "${HERALD_TEST_ALLOW_UNSAFE_DB_VOLUME:-0}" != "1" ]; then
                    echo "❌ Error: Found existing PostgreSQL volume from a previous installation, but ${ENV_FILE} is missing." >&2
                    echo "Generating a new random POSTGRES_PASSWORD will cause PostgreSQL to reject connections because the database volume already contains data initialized with the previous password." >&2
                    echo "To recover:" >&2
                    echo "  1. Restore your previous ${ENV_FILE} file containing the original POSTGRES_PASSWORD, OR" >&2
                    echo "  2. If you want a completely clean installation, reset Herald and remove volumes first:" >&2
                    echo "     ./scripts/reset-herald.sh --cold --remove-env" >&2
                    exit 1
                fi
            fi
        fi
    fi
}

validate_env_keys() {
    local required_keys=(
        "POSTGRES_DB"
        "POSTGRES_USER"
        "POSTGRES_PASSWORD"
        "HERALD_API_KEY"
        "TELEGRAM_BOT_TOKEN"
        "AI_PROVIDER"
    )
    for k in "${required_keys[@]}"; do
        local val
        val=$(get_env_val "$k")
        if [ -z "$val" ]; then
            echo "❌ Error: Required configuration key '${k}' is missing or empty in ${ENV_FILE}." >&2
            exit 1
        fi
    done
}

if [ "$START_ONLY" = false ]; then
    check_unsafe_db_volume

    if [ -f "$ENV_FILE" ]; then
        echo "ℹ️  Existing configuration found in ${ENV_FILE}."
        EXISTING_RES_M=$(get_env_val "GEMINI_RESEARCH_MODEL")
        if [ "$EXISTING_RES_M" = "gemini-2.5-flash" ]; then
            echo "🔄 Migrating GEMINI_RESEARCH_MODEL from former default gemini-2.5-flash to gemini-3.6-flash..."
            set_env_val "GEMINI_RESEARCH_MODEL" "gemini-3.6-flash"
        fi
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
    echo "  7) OpenAI (GPT-4o, GPT-4o-mini)"
    echo "  8) Anthropic (Claude 3.5 Sonnet / Haiku)"
    echo "  9) Ollama (Local LLM via HTTP)"
    prompt_value AI_CHOICE "Enter choice [1-9, default: 2]: " "2"

    case "$AI_CHOICE" in
        1)
            AI_PROVIDER="literal"
            set_env_val "AI_PROVIDER" "literal"
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
            set_env_val "GEMINI_RESEARCH_MODEL" "gemini-3.6-flash"
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
        7)
            AI_PROVIDER="openai"
            OPENAI_KEY=""
            while [ -z "$OPENAI_KEY" ]; do
                prompt_secret OPENAI_KEY "Enter your OpenAI API Key (sk-...): "
                OPENAI_KEY=$(trim_str "$OPENAI_KEY")
                if [ -z "$OPENAI_KEY" ]; then
                    echo "⚠️  OpenAI API Key cannot be empty."
                fi
            done
            set_env_val "AI_PROVIDER" "openai"
            set_env_val "OPENAI_API_KEY" "$OPENAI_KEY"
            set_env_val "OPENAI_MODEL" "gpt-4o"
            ;;
        8)
            AI_PROVIDER="anthropic"
            ANTHROPIC_KEY=""
            while [ -z "$ANTHROPIC_KEY" ]; do
                prompt_secret ANTHROPIC_KEY "Enter your Anthropic API Key (sk-ant-...): "
                ANTHROPIC_KEY=$(trim_str "$ANTHROPIC_KEY")
                if [ -z "$ANTHROPIC_KEY" ]; then
                    echo "⚠️  Anthropic API Key cannot be empty."
                fi
            done
            set_env_val "AI_PROVIDER" "anthropic"
            set_env_val "ANTHROPIC_API_KEY" "$ANTHROPIC_KEY"
            set_env_val "ANTHROPIC_MODEL" "claude-3-5-sonnet-20241022"
            ;;
        9)
            AI_PROVIDER="ollama"
            OLLAMA_URL=""
            prompt_value OLLAMA_URL "Enter Ollama Base URL [default: http://localhost:11434]: " "http://localhost:11434"
            OLLAMA_URL=$(trim_str "$OLLAMA_URL")
            set_env_val "AI_PROVIDER" "ollama"
            set_env_val "OLLAMA_BASE_URL" "$OLLAMA_URL"
            set_env_val "OLLAMA_MODEL" "llama3.1"
            ;;
        *)
            echo "❌ Error: Invalid AI provider choice '${AI_CHOICE}'." >&2
            exit 1
            ;;
    esac

    # Optional Failover Slots (Secondary & Tertiary Providers)
    if [ "$AI_PROVIDER" != "literal" ] && [ "$AI_PROVIDER" != "none" ]; then
        SEC_PROV=$(get_env_val "AI_SECONDARY_PROVIDER")
        if [ -z "$SEC_PROV" ] && [ "$NON_INTERACTIVE" = false ]; then
            echo ""
            prompt_value WANT_SEC "Configure an optional Secondary AI failover provider? [y/N]: " "N"
            if [[ "$WANT_SEC" =~ ^[Yy]$ ]]; then
                echo "Select Secondary AI Provider:"
                echo "  1) Google Gemini"
                echo "  2) Groq Cloud"
                echo "  3) OpenRouter"
                echo "  4) Mistral AI"
                echo "  5) Cloudflare Workers AI"
                echo "  6) OpenAI"
                echo "  7) Anthropic"
                echo "  8) Ollama"
                prompt_value SEC_CHOICE "Enter choice [1-8]: " ""
                case "$SEC_CHOICE" in
                    1)
                        if [ "$AI_PROVIDER" = "gemini" ]; then
                            echo "❌ Error: Duplicate provider 'gemini' is already Primary." >&2
                            exit 1
                        fi
                        set_env_val "AI_SECONDARY_PROVIDER" "gemini"
                        if [ -z "$(get_env_val "GEMINI_API_KEY")" ]; then
                            prompt_secret S_KEY "Enter Gemini API Key: "
                            set_env_val "GEMINI_API_KEY" "$(trim_str "$S_KEY")"
                        fi
                        if [ -z "$(get_env_val "GEMINI_MODEL")" ]; then set_env_val "GEMINI_MODEL" "gemini-3.5-flash"; fi
                        if [ -z "$(get_env_val "GEMINI_RESEARCH_MODEL")" ]; then set_env_val "GEMINI_RESEARCH_MODEL" "gemini-3.6-flash"; fi
                        ;;
                    2)
                        if [ "$AI_PROVIDER" = "groq" ]; then
                            echo "❌ Error: Duplicate provider 'groq' is already Primary." >&2
                            exit 1
                        fi
                        set_env_val "AI_SECONDARY_PROVIDER" "groq"
                        if [ -z "$(get_env_val "GROQ_API_KEY")" ]; then
                            prompt_secret S_KEY "Enter Groq API Key: "
                            set_env_val "GROQ_API_KEY" "$(trim_str "$S_KEY")"
                        fi
                        if [ -z "$(get_env_val "GROQ_MODEL")" ]; then set_env_val "GROQ_MODEL" "llama-3.3-70b-versatile"; fi
                        ;;
                    3)
                        if [ "$AI_PROVIDER" = "openrouter" ]; then
                            echo "❌ Error: Duplicate provider 'openrouter' is already Primary." >&2
                            exit 1
                        fi
                        set_env_val "AI_SECONDARY_PROVIDER" "openrouter"
                        if [ -z "$(get_env_val "OPENROUTER_API_KEY")" ]; then
                            prompt_secret S_KEY "Enter OpenRouter API Key: "
                            set_env_val "OPENROUTER_API_KEY" "$(trim_str "$S_KEY")"
                        fi
                        if [ -z "$(get_env_val "OPENROUTER_MODEL")" ]; then set_env_val "OPENROUTER_MODEL" "meta-llama/llama-3.3-70b-instruct"; fi
                        ;;
                    4)
                        if [ "$AI_PROVIDER" = "mistral" ]; then
                            echo "❌ Error: Duplicate provider 'mistral' is already Primary." >&2
                            exit 1
                        fi
                        set_env_val "AI_SECONDARY_PROVIDER" "mistral"
                        if [ -z "$(get_env_val "MISTRAL_API_KEY")" ]; then
                            prompt_secret S_KEY "Enter Mistral API Key: "
                            set_env_val "MISTRAL_API_KEY" "$(trim_str "$S_KEY")"
                        fi
                        if [ -z "$(get_env_val "MISTRAL_MODEL")" ]; then set_env_val "MISTRAL_MODEL" "mistral-large-latest"; fi
                        ;;
                    5)
                        if [ "$AI_PROVIDER" = "cloudflare" ]; then
                            echo "❌ Error: Duplicate provider 'cloudflare' is already Primary." >&2
                            exit 1
                        fi
                        set_env_val "AI_SECONDARY_PROVIDER" "cloudflare"
                        if [ -z "$(get_env_val "CLOUDFLARE_API_TOKEN")" ]; then
                            prompt_secret S_TOK "Enter Cloudflare API Token: "
                            set_env_val "CLOUDFLARE_API_TOKEN" "$(trim_str "$S_TOK")"
                        fi
                        if [ -z "$(get_env_val "CLOUDFLARE_ACCOUNT_ID")" ]; then
                            prompt_value S_ACC "Enter Cloudflare Account ID: "
                            set_env_val "CLOUDFLARE_ACCOUNT_ID" "$(trim_str "$S_ACC")"
                        fi
                        if [ -z "$(get_env_val "CLOUDFLARE_AI_MODEL")" ]; then set_env_val "CLOUDFLARE_AI_MODEL" "@cf/meta/llama-3.3-70b-instruct-fp8-fast"; fi
                        ;;
                    6)
                        if [ "$AI_PROVIDER" = "openai" ]; then
                            echo "❌ Error: Duplicate provider 'openai' is already Primary." >&2
                            exit 1
                        fi
                        set_env_val "AI_SECONDARY_PROVIDER" "openai"
                        if [ -z "$(get_env_val "OPENAI_API_KEY")" ]; then
                            prompt_secret S_KEY "Enter OpenAI API Key: "
                            set_env_val "OPENAI_API_KEY" "$(trim_str "$S_KEY")"
                        fi
                        if [ -z "$(get_env_val "OPENAI_MODEL")" ]; then set_env_val "OPENAI_MODEL" "gpt-4o"; fi
                        ;;
                    7)
                        if [ "$AI_PROVIDER" = "anthropic" ]; then
                            echo "❌ Error: Duplicate provider 'anthropic' is already Primary." >&2
                            exit 1
                        fi
                        set_env_val "AI_SECONDARY_PROVIDER" "anthropic"
                        if [ -z "$(get_env_val "ANTHROPIC_API_KEY")" ]; then
                            prompt_secret S_KEY "Enter Anthropic API Key: "
                            set_env_val "ANTHROPIC_API_KEY" "$(trim_str "$S_KEY")"
                        fi
                        if [ -z "$(get_env_val "ANTHROPIC_MODEL")" ]; then set_env_val "ANTHROPIC_MODEL" "claude-3-5-sonnet-20241022"; fi
                        ;;
                    8)
                        if [ "$AI_PROVIDER" = "ollama" ]; then
                            echo "❌ Error: Duplicate provider 'ollama' is already Primary." >&2
                            exit 1
                        fi
                        set_env_val "AI_SECONDARY_PROVIDER" "ollama"
                        if [ -z "$(get_env_val "OLLAMA_BASE_URL")" ]; then
                            prompt_value S_URL "Enter Ollama Base URL: " "http://localhost:11434"
                            set_env_val "OLLAMA_BASE_URL" "$(trim_str "$S_URL")"
                        fi
                        if [ -z "$(get_env_val "OLLAMA_MODEL")" ]; then set_env_val "OLLAMA_MODEL" "llama3.1"; fi
                        ;;
                    *)
                        echo "❌ Error: Invalid Secondary AI provider choice. Literal is not permitted as Secondary." >&2
                        exit 1
                        ;;
                esac
            fi
        fi

        SEC_PROV=$(get_env_val "AI_SECONDARY_PROVIDER")
        if [ -n "$SEC_PROV" ] && [ "$SEC_PROV" != "literal" ] && [ "$SEC_PROV" != "none" ]; then
            TERT_PROV=$(get_env_val "AI_TERTIARY_PROVIDER")
            if [ -z "$TERT_PROV" ] && [ "$NON_INTERACTIVE" = false ]; then
                echo ""
                prompt_value WANT_TERT "Configure an optional Tertiary AI failover provider? [y/N]: " "N"
                if [[ "$WANT_TERT" =~ ^[Yy]$ ]]; then
                    echo "Select Tertiary AI Provider:"
                    echo "  1) Google Gemini"
                    echo "  2) Groq Cloud"
                    echo "  3) OpenRouter"
                    echo "  4) Mistral AI"
                    echo "  5) Cloudflare Workers AI"
                    echo "  6) OpenAI"
                    echo "  7) Anthropic"
                    echo "  8) Ollama"
                    prompt_value TERT_CHOICE "Enter choice [1-8]: " ""
                    case "$TERT_CHOICE" in
                        1)
                            if [ "$AI_PROVIDER" = "gemini" ] || [ "$SEC_PROV" = "gemini" ]; then
                                echo "❌ Error: Duplicate provider 'gemini' is already in failover chain." >&2
                                exit 1
                            fi
                            set_env_val "AI_TERTIARY_PROVIDER" "gemini"
                            if [ -z "$(get_env_val "GEMINI_API_KEY")" ]; then
                                prompt_secret T_KEY "Enter Gemini API Key: "
                                set_env_val "GEMINI_API_KEY" "$(trim_str "$T_KEY")"
                            fi
                            if [ -z "$(get_env_val "GEMINI_MODEL")" ]; then set_env_val "GEMINI_MODEL" "gemini-3.5-flash"; fi
                            if [ -z "$(get_env_val "GEMINI_RESEARCH_MODEL")" ]; then set_env_val "GEMINI_RESEARCH_MODEL" "gemini-3.6-flash"; fi
                            ;;
                        2)
                            if [ "$AI_PROVIDER" = "groq" ] || [ "$SEC_PROV" = "groq" ]; then
                                echo "❌ Error: Duplicate provider 'groq' is already in failover chain." >&2
                                exit 1
                            fi
                            set_env_val "AI_TERTIARY_PROVIDER" "groq"
                            if [ -z "$(get_env_val "GROQ_API_KEY")" ]; then
                                prompt_secret T_KEY "Enter Groq API Key: "
                                set_env_val "GROQ_API_KEY" "$(trim_str "$T_KEY")"
                            fi
                            if [ -z "$(get_env_val "GROQ_MODEL")" ]; then set_env_val "GROQ_MODEL" "llama-3.3-70b-versatile"; fi
                            ;;
                        3)
                            if [ "$AI_PROVIDER" = "openrouter" ] || [ "$SEC_PROV" = "openrouter" ]; then
                                echo "❌ Error: Duplicate provider 'openrouter' is already in failover chain." >&2
                                exit 1
                            fi
                            set_env_val "AI_TERTIARY_PROVIDER" "openrouter"
                            if [ -z "$(get_env_val "OPENROUTER_API_KEY")" ]; then
                                prompt_secret T_KEY "Enter OpenRouter API Key: "
                                set_env_val "OPENROUTER_API_KEY" "$(trim_str "$T_KEY")"
                            fi
                            if [ -z "$(get_env_val "OPENROUTER_MODEL")" ]; then set_env_val "OPENROUTER_MODEL" "meta-llama/llama-3.3-70b-instruct"; fi
                            ;;
                        4)
                            if [ "$AI_PROVIDER" = "mistral" ] || [ "$SEC_PROV" = "mistral" ]; then
                                echo "❌ Error: Duplicate provider 'mistral' is already in failover chain." >&2
                                exit 1
                            fi
                            set_env_val "AI_TERTIARY_PROVIDER" "mistral"
                            if [ -z "$(get_env_val "MISTRAL_API_KEY")" ]; then
                                prompt_secret T_KEY "Enter Mistral API Key: "
                                set_env_val "MISTRAL_API_KEY" "$(trim_str "$T_KEY")"
                            fi
                            if [ -z "$(get_env_val "MISTRAL_MODEL")" ]; then set_env_val "MISTRAL_MODEL" "mistral-large-latest"; fi
                            ;;
                        5)
                            if [ "$AI_PROVIDER" = "cloudflare" ] || [ "$SEC_PROV" = "cloudflare" ]; then
                                echo "❌ Error: Duplicate provider 'cloudflare' is already in failover chain." >&2
                                exit 1
                            fi
                            set_env_val "AI_TERTIARY_PROVIDER" "cloudflare"
                            if [ -z "$(get_env_val "CLOUDFLARE_API_TOKEN")" ]; then
                                prompt_secret T_TOK "Enter Cloudflare API Token: "
                                set_env_val "CLOUDFLARE_API_TOKEN" "$(trim_str "$T_TOK")"
                            fi
                            if [ -z "$(get_env_val "CLOUDFLARE_ACCOUNT_ID")" ]; then
                                prompt_value T_ACC "Enter Cloudflare Account ID: "
                                set_env_val "CLOUDFLARE_ACCOUNT_ID" "$(trim_str "$T_ACC")"
                            fi
                            if [ -z "$(get_env_val "CLOUDFLARE_AI_MODEL")" ]; then set_env_val "CLOUDFLARE_AI_MODEL" "@cf/meta/llama-3.3-70b-instruct-fp8-fast"; fi
                            ;;
                        6)
                            if [ "$AI_PROVIDER" = "openai" ] || [ "$SEC_PROV" = "openai" ]; then
                                echo "❌ Error: Duplicate provider 'openai' is already in failover chain." >&2
                                exit 1
                            fi
                            set_env_val "AI_TERTIARY_PROVIDER" "openai"
                            if [ -z "$(get_env_val "OPENAI_API_KEY")" ]; then
                                prompt_secret T_KEY "Enter OpenAI API Key: "
                                set_env_val "OPENAI_API_KEY" "$(trim_str "$T_KEY")"
                            fi
                            if [ -z "$(get_env_val "OPENAI_MODEL")" ]; then set_env_val "OPENAI_MODEL" "gpt-4o"; fi
                            ;;
                        7)
                            if [ "$AI_PROVIDER" = "anthropic" ] || [ "$SEC_PROV" = "anthropic" ]; then
                                echo "❌ Error: Duplicate provider 'anthropic' is already in failover chain." >&2
                                exit 1
                            fi
                            set_env_val "AI_TERTIARY_PROVIDER" "anthropic"
                            if [ -z "$(get_env_val "ANTHROPIC_API_KEY")" ]; then
                                prompt_secret T_KEY "Enter Anthropic API Key: "
                                set_env_val "ANTHROPIC_API_KEY" "$(trim_str "$T_KEY")"
                            fi
                            if [ -z "$(get_env_val "ANTHROPIC_MODEL")" ]; then set_env_val "ANTHROPIC_MODEL" "claude-3-5-sonnet-20241022"; fi
                            ;;
                        8)
                            if [ "$AI_PROVIDER" = "ollama" ] || [ "$SEC_PROV" = "ollama" ]; then
                                echo "❌ Error: Duplicate provider 'ollama' is already in failover chain." >&2
                                exit 1
                            fi
                            set_env_val "AI_TERTIARY_PROVIDER" "ollama"
                            if [ -z "$(get_env_val "OLLAMA_BASE_URL")" ]; then
                                prompt_value T_URL "Enter Ollama Base URL: " "http://localhost:11434"
                                set_env_val "OLLAMA_BASE_URL" "$(trim_str "$T_URL")"
                            fi
                            if [ -z "$(get_env_val "OLLAMA_MODEL")" ]; then set_env_val "OLLAMA_MODEL" "llama3.1"; fi
                            ;;
                        *)
                            echo "❌ Error: Invalid Tertiary AI provider choice. Literal is not permitted as Tertiary." >&2
                            exit 1
                            ;;
                    esac
                fi
            fi
        fi
    fi
else
    case "$AI_PROVIDER" in
        none|literal|gemini|groq|openrouter|mistral|cloudflare|openai|anthropic|ollama)
            echo "✅ AI Provider is configured: ${AI_PROVIDER}"
            ;;
        *)
            echo "❌ Error: Unknown AI_PROVIDER '${AI_PROVIDER}' in ${ENV_FILE}." >&2
            echo "Allowed values: literal, gemini, groq, openrouter, mistral, cloudflare, openai, anthropic, ollama." >&2
            exit 1
            ;;
    esac
fi

# Function to validate credentials and connectivity for an AI provider candidate
validate_provider_candidate() {
    local prov="$1"
    local slot_name="${2:-Primary}"
    case "$prov" in
        gemini)
            local g_key g_mod gem_resp
            g_key=$(get_env_val "GEMINI_API_KEY")
            g_mod=$(get_env_val "GEMINI_MODEL")
            g_mod=${g_mod:-"gemini-3.5-flash"}
            if [ -z "$g_key" ]; then
                echo "❌ Error: [${slot_name}] Gemini API key is missing." >&2
                return 1
            fi
            gem_resp=$(printf 'url = "https://generativelanguage.googleapis.com/v1beta/models/%s"\nheader = "x-goog-api-key: %s"\n' "$g_mod" "$g_key" | call_curl_config)
            if echo "$gem_resp" | grep -q '"name":'; then
                echo "✅ [${slot_name}] Gemini API connection and model '${g_mod}' verified."
                return 0
            else
                echo "⚠️  [${slot_name}] Gemini verification for '${g_mod}' failed." >&2
                return 1
            fi
            ;;
        groq)
            local gr_key gr_mod gr_resp
            gr_key=$(get_env_val "GROQ_API_KEY")
            gr_mod=$(get_env_val "GROQ_MODEL")
            gr_mod=${gr_mod:-"llama-3.3-70b-versatile"}
            if [ -z "$gr_key" ]; then
                echo "❌ Error: [${slot_name}] Groq API key is missing." >&2
                return 1
            fi
            gr_resp=$(printf 'url = "https://api.groq.com/openai/v1/models/%s"\nheader = "Authorization: Bearer %s"\n' "$gr_mod" "$gr_key" | call_curl_config)
            if echo "$gr_resp" | grep -q '"id":'; then
                echo "✅ [${slot_name}] Groq Cloud connection and model '${gr_mod}' verified."
                return 0
            else
                echo "⚠️  [${slot_name}] Groq verification for '${gr_mod}' failed." >&2
                return 1
            fi
            ;;
        openrouter)
            local or_k or_mod or_resp
            or_k=$(get_env_val "OPENROUTER_API_KEY")
            or_mod=$(get_env_val "OPENROUTER_MODEL")
            or_mod=${or_mod:-"meta-llama/llama-3.3-70b-instruct"}
            if [ -z "$or_k" ]; then
                echo "❌ Error: [${slot_name}] OpenRouter API key is missing." >&2
                return 1
            fi
            or_resp=$(printf 'url = "https://openrouter.ai/api/v1/models"\nheader = "Authorization: Bearer %s"\n' "$or_k" | call_curl_config)
            if echo "$or_resp" | grep -q "${or_mod}"; then
                echo "✅ [${slot_name}] OpenRouter connection and model '${or_mod}' verified."
                return 0
            else
                echo "⚠️  [${slot_name}] OpenRouter verification for '${or_mod}' failed." >&2
                return 1
            fi
            ;;
        mistral)
            local m_k m_mod m_resp
            m_k=$(get_env_val "MISTRAL_API_KEY")
            m_mod=$(get_env_val "MISTRAL_MODEL")
            m_mod=${m_mod:-"mistral-large-latest"}
            if [ -z "$m_k" ]; then
                echo "❌ Error: [${slot_name}] Mistral API key is missing." >&2
                return 1
            fi
            m_resp=$(printf 'url = "https://api.mistral.ai/v1/models/%s"\nheader = "Authorization: Bearer %s"\n' "$m_mod" "$m_k" | call_curl_config)
            if echo "$m_resp" | grep -q '"id":'; then
                echo "✅ [${slot_name}] Mistral AI connection and model '${m_mod}' verified."
                return 0
            else
                echo "⚠️  [${slot_name}] Mistral verification for '${m_mod}' failed." >&2
                return 1
            fi
            ;;
        cloudflare)
            local cf_t cf_a cf_mod cf_resp
            cf_t=$(get_env_val "CLOUDFLARE_API_TOKEN")
            cf_a=$(get_env_val "CLOUDFLARE_ACCOUNT_ID")
            cf_mod=$(get_env_val "CLOUDFLARE_AI_MODEL")
            cf_mod=${cf_mod:-$(get_env_val "CLOUDFLARE_MODEL")}
            cf_mod=${cf_mod:-"@cf/meta/llama-3.3-70b-instruct-fp8-fast"}
            if [ -z "$cf_t" ] || [ -z "$cf_a" ]; then
                echo "❌ Error: [${slot_name}] Cloudflare API Token or Account ID is missing." >&2
                return 1
            fi
            cf_resp=$(printf 'url = "https://api.cloudflare.com/client/v4/accounts/%s/ai/models/search?search=%s"\nheader = "Authorization: Bearer %s"\n' "$cf_a" "$cf_mod" "$cf_t" | call_curl_config)
            if echo "$cf_resp" | grep -q '"success":true' && echo "$cf_resp" | grep -q "${cf_mod}"; then
                echo "✅ [${slot_name}] Cloudflare Workers AI connection and model '${cf_mod}' verified."
                return 0
            else
                echo "⚠️  [${slot_name}] Cloudflare verification for '${cf_mod}' failed." >&2
                return 1
            fi
            ;;
        openai)
            local o_key o_mod o_resp
            o_key=$(get_env_val "OPENAI_API_KEY")
            o_mod=$(get_env_val "OPENAI_MODEL")
            o_mod=${o_mod:-"gpt-4o"}
            if [ -z "$o_key" ]; then
                echo "❌ Error: [${slot_name}] OpenAI API key is missing." >&2
                return 1
            fi
            o_resp=$(printf 'url = "https://api.openai.com/v1/models/%s"\nheader = "Authorization: Bearer %s"\n' "$o_mod" "$o_key" | call_curl_config)
            if echo "$o_resp" | grep -q '"id":'; then
                echo "✅ [${slot_name}] OpenAI connection and model '${o_mod}' verified."
                return 0
            else
                echo "⚠️  [${slot_name}] OpenAI verification for '${o_mod}' failed." >&2
                return 1
            fi
            ;;
        anthropic)
            local a_key a_mod a_resp
            a_key=$(get_env_val "ANTHROPIC_API_KEY")
            a_mod=$(get_env_val "ANTHROPIC_MODEL")
            a_mod=${a_mod:-"claude-3-5-sonnet-20241022"}
            if [ -z "$a_key" ]; then
                echo "❌ Error: [${slot_name}] Anthropic API key is missing." >&2
                return 1
            fi
            a_resp=$(printf 'url = "https://api.anthropic.com/v1/models/%s"\nheader = "x-api-key: %s"\nheader = "anthropic-version: 2023-06-01"\n' "$a_mod" "$a_key" | call_curl_config)
            if echo "$a_resp" | grep -q '"id":'; then
                echo "✅ [${slot_name}] Anthropic connection and model '${a_mod}' verified."
                return 0
            else
                echo "⚠️  [${slot_name}] Anthropic verification for '${a_mod}' failed." >&2
                return 1
            fi
            ;;
        ollama)
            local ol_url ol_mod ol_resp
            ol_url=$(get_env_val "OLLAMA_BASE_URL")
            ol_url=${ol_url:-"http://localhost:11434"}
            ol_mod=$(get_env_val "OLLAMA_MODEL")
            ol_mod=${ol_mod:-"llama3.1"}
            ol_resp=$(printf 'url = "%s/api/tags"\n' "$ol_url" | call_curl_config)
            if echo "$ol_resp" | grep -q '"models":'; then
                echo "✅ [${slot_name}] Ollama connection verified at ${ol_url}."
                return 0
            else
                echo "⚠️  [${slot_name}] Ollama verification at ${ol_url} failed." >&2
                return 1
            fi
            ;;
        literal|none)
            if [ "$slot_name" != "Primary" ]; then
                echo "❌ Error: Literal is disallowed as Secondary or Tertiary provider." >&2
                return 1
            fi
            echo "ℹ️  Literal mode selected (no external AI provider calls)."
            return 0
            ;;
        *)
            echo "❌ Error: [${slot_name}] Unknown AI provider '${prov}'." >&2
            return 1
            ;;
    esac
}

# 3. Live Validate Finished Provider Chain without Echoing Secrets
echo ""
echo "🔍 Validating AI Provider failover chain and model availability..."

AI_PROVIDER=$(get_env_val "AI_PROVIDER")
SEC_PROV=$(get_env_val "AI_SECONDARY_PROVIDER")
TERT_PROV=$(get_env_val "AI_TERTIARY_PROVIDER")

# Validate chain integrity:
if [ "$AI_PROVIDER" = "literal" ] || [ "$AI_PROVIDER" = "none" ]; then
    if [ -n "$SEC_PROV" ] || [ -n "$TERT_PROV" ]; then
        echo "❌ Error: Literal provider cannot have Secondary or Tertiary failover candidates." >&2
        exit 1
    fi
fi

if [ -n "$SEC_PROV" ]; then
    if [ "$SEC_PROV" = "literal" ] || [ "$SEC_PROV" = "none" ]; then
        echo "❌ Error: Literal is disallowed as Secondary provider." >&2
        exit 1
    fi
    if [ "$SEC_PROV" = "$AI_PROVIDER" ]; then
        echo "❌ Error: Duplicate provider '${SEC_PROV}' configured in Primary and Secondary slots." >&2
        exit 1
    fi
fi

if [ -n "$TERT_PROV" ]; then
    if [ "$TERT_PROV" = "literal" ] || [ "$TERT_PROV" = "none" ]; then
        echo "❌ Error: Literal is disallowed as Tertiary provider." >&2
        exit 1
    fi
    if [ -z "$SEC_PROV" ]; then
        echo "❌ Error: Tertiary provider configured without Secondary provider." >&2
        exit 1
    fi
    if [ "$TERT_PROV" = "$AI_PROVIDER" ] || [ "$TERT_PROV" = "$SEC_PROV" ]; then
        echo "❌ Error: Duplicate provider '${TERT_PROV}' in Tertiary slot." >&2
        exit 1
    fi
fi

# Validate credentials for each candidate in the chain (fail hard, never silently rewrite to Literal)
if ! validate_provider_candidate "$AI_PROVIDER" "Primary"; then
    echo "❌ Error: Primary AI provider '${AI_PROVIDER}' validation failed." >&2
    echo "Please check your configuration or credentials and rerun setup.sh." >&2
    exit 1
fi

if [ -n "$SEC_PROV" ]; then
    if ! validate_provider_candidate "$SEC_PROV" "Secondary"; then
        echo "❌ Error: Secondary AI provider '${SEC_PROV}' validation failed." >&2
        echo "Please check your configuration or credentials and rerun setup.sh." >&2
        exit 1
    fi
fi

if [ -n "$TERT_PROV" ]; then
    if ! validate_provider_candidate "$TERT_PROV" "Tertiary"; then
        echo "❌ Error: Tertiary AI provider '${TERT_PROV}' validation failed." >&2
        echo "Please check your configuration or credentials and rerun setup.sh." >&2
        exit 1
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
    if [ -z "$(get_env_val "HERALD_DNS_PRIMARY")" ]; then set_env_val "HERALD_DNS_PRIMARY" "1.1.1.1"; fi
    if [ -z "$(get_env_val "HERALD_DNS_SECONDARY")" ]; then set_env_val "HERALD_DNS_SECONDARY" "8.8.8.8"; fi
    if [ -z "$(get_env_val "DIAGNOSTICS_RETENTION_DAYS")" ]; then set_env_val "DIAGNOSTICS_RETENTION_DAYS" "30"; fi

    chmod 600 "$ENV_FILE" 2>/dev/null || true
    echo "✅ Configuration file (${ENV_FILE}) is up to date (permissions: 0600)."

    validate_env_keys

    if [ "$CONFIGURE_ONLY" = true ]; then
        echo "✅ Herald configuration complete (--configure-only)."
        exit 0
    fi
fi

# 5. Start Herald core services and strictly verify startup (NO false success on failure)
if [ "$CONFIGURE_ONLY" = false ]; then
    if [ "$START_ONLY" = true ]; then
        if [ ! -f "$ENV_FILE" ]; then
            echo "❌ Error: Configuration file '${ENV_FILE}' not found. Run './setup.sh --configure-only' first." >&2
            exit 1
        fi
        validate_env_keys
        if [ -z "${BOT_NAME:-}" ]; then
            TG_TOKEN=$(get_env_val "TELEGRAM_BOT_TOKEN")
            if [ -n "$TG_TOKEN" ]; then
                TG_ME_RESP=$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$TG_TOKEN" | call_curl_config)
                BOT_NAME=$(echo "$TG_ME_RESP" | grep -o '"username":"[^"]*' | cut -d'"' -f4 || echo "HeraldBot")
            fi
            BOT_NAME="${BOT_NAME:-HeraldBot}"
        fi
    fi

    echo ""
    echo "🚀 Starting Herald core services via Docker Compose..."

    mkdir -p logs/diagnostics
    chmod 755 logs logs/diagnostics 2>/dev/null || true

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
        echo "🔊 Prewarming Kokoro voice sample cache in herald-worker..."
        if docker compose exec -T herald-worker python -m herald.services.voice_manager --prewarm; then
            echo "✅ Voice sample cache prewarmed."
        else
            echo "❌ Error: Voice sample cache prewarming failed." >&2
            exit 1
        fi
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

if [ "$NO_BANNER" = false ]; then
    echo ""
    echo "========================================================"
    echo "               Herald Setup Complete! /logs for details"
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
    echo "/download     - Download completed podcast MP3 document"
    echo "/status       - System health, queue depth, and uptime"
    echo "/ai_check     - AI provider connection test"
    echo "/queue        - Pending and processing jobs"
    echo "/settings     - Preferences, default voice, and pre-TTS confirmation toggle"
    echo "/diagnostics  - View job diagnostics and download the sanitized support bundle"
    echo "/readme       - Project documentation"
    echo ""
    echo "SERVER COMMANDS"
    echo "Live logs: docker compose logs -f --tail=100"
    echo "Status:    docker compose ps"
    echo "Stop:      docker compose down"
    echo "Start:     docker compose up -d"
    echo "========================================================"
fi
fi

