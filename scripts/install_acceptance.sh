#!/usr/bin/env bash
set -euo pipefail

# Herald Installation Acceptance Validation Helper
# Verifies installation health, dynamic schema revision, service state, permissions, and isolation without exposing secrets.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Normalize explicit HERALD_ENV_FILE to absolute path before changing directory
if [ -n "${HERALD_ENV_FILE:-}" ]; then
    if [[ "$HERALD_ENV_FILE" != /* ]] && [[ "$HERALD_ENV_FILE" != ?:/* ]] && [[ "$HERALD_ENV_FILE" != ?:\\* ]]; then
        ENV_FILE="$(pwd)/${HERALD_ENV_FILE}"
    else
        ENV_FILE="$HERALD_ENV_FILE"
    fi
else
    ENV_FILE="${SCRIPT_DIR}/.env"
fi

# Change working directory to repository root for all relative compose operations
cd "$SCRIPT_DIR"

FAILURES=0
PG_CID=""
KOKORO_CID=""
DYNAMIC_HEAD=""
LIVE_REV=""

report_pass() {
    echo "  ✅ $1"
}

report_fail() {
    echo "  ❌ $1" >&2
    FAILURES=$((FAILURES + 1))
}

echo "========================================================"
echo "      🔍  Herald Installation Acceptance Validation     "
echo "========================================================"
echo ""

# 1. Verify .env Existence and Permissions (0600)
echo "[1/8] Checking configuration file and permissions..."
if [ ! -f "$ENV_FILE" ]; then
    report_fail "Configuration file '${ENV_FILE}' not found."
else
    if [ "${HERALD_TEST_ALLOW_PERMS:-0}" = "1" ]; then
        report_pass "Configuration file exists (permission check bypassed for test harness)."
    else
        PERMS=$(stat -c "%a" "$ENV_FILE" 2>/dev/null || stat -f "%Lp" "$ENV_FILE" 2>/dev/null || echo "")
        if [ "$PERMS" = "600" ] || [ "$PERMS" = "0600" ]; then
            report_pass "Configuration file exists with strict 0600 permissions."
        else
            report_fail "Configuration file permissions are '${PERMS}', expected '0600'."
        fi
    fi
fi

# Pure-bash helper to read .env variable safely without sourcing or xargs
get_env_key() {
    local key="$1"
    if [ -f "$ENV_FILE" ]; then
        local raw
        raw=$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d'=' -f2- || true)
        raw="${raw%\"}"
        raw="${raw#\"}"
        raw="${raw%\'}"
        raw="${raw#\'}"
        raw="${raw#"${raw%%[![:space:]]*}"}"
        raw="${raw%"${raw##*[![:space:]]}"}"
        printf "%s" "$raw"
    fi
}

# 2. Check for Placeholder Secrets and Provider Configuration
echo "[2/8] Auditing credentials and AI provider consistency..."
KNOWN_PLACEHOLDERS=(
    "your-telegram-bot-token-from-botfather"
    "herald_secure_password"
    "change-this-to-a-secure-random-db-password"
    "change-this-to-a-secure-random-api-key"
)

TG_TOKEN=$(get_env_key "TELEGRAM_BOT_TOKEN")
if [ -z "$TG_TOKEN" ]; then
    report_fail "TELEGRAM_BOT_TOKEN is missing or empty in ${ENV_FILE}."
else
    is_placeholder=false
    for p in "${KNOWN_PLACEHOLDERS[@]}"; do
        if [ "$TG_TOKEN" = "$p" ]; then is_placeholder=true; break; fi
    done
    if [ "$is_placeholder" = true ]; then
        report_fail "TELEGRAM_BOT_TOKEN matches a known default placeholder."
    else
        report_pass "TELEGRAM_BOT_TOKEN is present and configured."
    fi
fi

DB_PASS=$(get_env_key "POSTGRES_PASSWORD")
if [ -z "$DB_PASS" ]; then
    report_fail "POSTGRES_PASSWORD is missing or empty in ${ENV_FILE}."
else
    is_placeholder=false
    for p in "${KNOWN_PLACEHOLDERS[@]}"; do
        if [ "$DB_PASS" = "$p" ]; then is_placeholder=true; break; fi
    done
    if [ "$is_placeholder" = true ]; then
        report_fail "POSTGRES_PASSWORD matches a known default placeholder."
    else
        report_pass "POSTGRES_PASSWORD is present and configured."
    fi
fi

HERALD_KEY=$(get_env_key "HERALD_API_KEY")
if [ -z "$HERALD_KEY" ]; then
    report_fail "HERALD_API_KEY is missing or empty in ${ENV_FILE}."
else
    is_placeholder=false
    for p in "${KNOWN_PLACEHOLDERS[@]}"; do
        if [ "$HERALD_KEY" = "$p" ]; then is_placeholder=true; break; fi
    done
    if [ "$is_placeholder" = true ]; then
        report_fail "HERALD_API_KEY matches a known default placeholder."
    else
        report_pass "HERALD_API_KEY is present and configured."
    fi
fi

audit_provider_credentials() {
    local prov="$1"
    local slot="$2"

    if [ -z "$prov" ]; then
        return 0
    fi

    case "$prov" in
        literal|none)
            report_pass "[${slot}] Literal mode active (no external AI provider key required)."
            ;;
        gemini)
            local g_key
            g_key=$(get_env_key "GEMINI_API_KEY")
            if [ -z "$g_key" ]; then
                report_fail "[${slot}] AI provider is 'gemini' but GEMINI_API_KEY is missing."
            else
                report_pass "[${slot}] Gemini API credentials configured."
            fi
            ;;
        groq)
            local gr_key
            gr_key=$(get_env_key "GROQ_API_KEY")
            if [ -z "$gr_key" ]; then
                report_fail "[${slot}] AI provider is 'groq' but GROQ_API_KEY is missing."
            else
                report_pass "[${slot}] Groq API credentials configured."
            fi
            ;;
        openrouter)
            local or_key
            or_key=$(get_env_key "OPENROUTER_API_KEY")
            if [ -z "$or_key" ]; then
                report_fail "[${slot}] AI provider is 'openrouter' but OPENROUTER_API_KEY is missing."
            else
                report_pass "[${slot}] OpenRouter API credentials configured."
            fi
            ;;
        mistral)
            local m_key
            m_key=$(get_env_key "MISTRAL_API_KEY")
            if [ -z "$m_key" ]; then
                report_fail "[${slot}] AI provider is 'mistral' but MISTRAL_API_KEY is missing."
            else
                report_pass "[${slot}] Mistral API credentials configured."
            fi
            ;;
        cloudflare)
            local cf_t cf_a
            cf_t=$(get_env_key "CLOUDFLARE_API_TOKEN")
            cf_a=$(get_env_key "CLOUDFLARE_ACCOUNT_ID")
            if [ -z "$cf_t" ] || [ -z "$cf_a" ]; then
                report_fail "[${slot}] Cloudflare API Token or Account ID is missing."
            else
                report_pass "[${slot}] Cloudflare Workers AI credentials configured."
            fi
            ;;
        openai)
            local oa_key
            oa_key=$(get_env_key "OPENAI_API_KEY")
            if [ -z "$oa_key" ]; then
                report_fail "[${slot}] AI provider is 'openai' but OPENAI_API_KEY is missing."
            else
                report_pass "[${slot}] OpenAI API credentials configured."
            fi
            ;;
        anthropic)
            local ant_key
            ant_key=$(get_env_key "ANTHROPIC_API_KEY")
            if [ -z "$ant_key" ]; then
                report_fail "[${slot}] AI provider is 'anthropic' but ANTHROPIC_API_KEY is missing."
            else
                report_pass "[${slot}] Anthropic API credentials configured."
            fi
            ;;
        ollama)
            local ol_url
            ol_url=$(get_env_key "OLLAMA_BASE_URL")
            if [ -z "$ol_url" ]; then
                report_fail "[${slot}] AI provider is 'ollama' but OLLAMA_BASE_URL is missing."
            else
                report_pass "[${slot}] Ollama base URL configured."
            fi
            ;;
        *)
            report_fail "[${slot}] Unknown AI provider '${prov}' configured in ${ENV_FILE}."
            ;;
    esac
}

AI_PRIMARY=$(get_env_key "AI_PROVIDER")
AI_PRIMARY=${AI_PRIMARY:-"literal"}
audit_provider_credentials "$AI_PRIMARY" "Primary"

AI_SEC=$(get_env_key "AI_SECONDARY_PROVIDER")
if [ -n "$AI_SEC" ]; then
    if [ "$AI_SEC" = "literal" ] || [ "$AI_SEC" = "none" ]; then
        report_fail "[Secondary] Literal is not allowed as Secondary provider."
    elif [ "$AI_SEC" = "$AI_PRIMARY" ]; then
        report_fail "[Secondary] Duplicate provider '${AI_SEC}' matches Primary."
    else
        audit_provider_credentials "$AI_SEC" "Secondary"
    fi
fi

AI_TERT=$(get_env_key "AI_TERTIARY_PROVIDER")
if [ -n "$AI_TERT" ]; then
    if [ "$AI_TERT" = "literal" ] || [ "$AI_TERT" = "none" ]; then
        report_fail "[Tertiary] Literal is not allowed as Tertiary provider."
    elif [ "$AI_TERT" = "$AI_PRIMARY" ] || [ "$AI_TERT" = "$AI_SEC" ]; then
        report_fail "[Tertiary] Duplicate provider '${AI_TERT}' in failover chain."
    else
        audit_provider_credentials "$AI_TERT" "Tertiary"
    fi
fi

# 3. Check Default Service States
echo "[3/8] Verifying default container service states..."
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    report_fail "Docker Engine or Docker Compose v2 is not available."
else
    # PostgreSQL health
    PG_CID=$(docker compose ps -q postgres 2>/dev/null || true)
    if [ -z "$PG_CID" ]; then
        report_fail "PostgreSQL container (postgres) is not running."
    else
        PG_HEALTH=$(docker inspect --format='{{json .State.Health.Status}}' "$PG_CID" 2>/dev/null | tr -d '"' || echo "unknown")
        if [ "$PG_HEALTH" = "healthy" ]; then
            report_pass "PostgreSQL container is running and healthy."
        else
            report_fail "PostgreSQL container health is '${PG_HEALTH}', expected 'healthy'."
        fi
    fi

    # Kokoro health
    KOKORO_CID=$(docker compose ps -q kokoro 2>/dev/null || true)
    if [ -z "$KOKORO_CID" ]; then
        report_fail "Kokoro TTS container (kokoro) is not running."
    else
        K_HEALTH=$(docker inspect --format='{{json .State.Health.Status}}' "$KOKORO_CID" 2>/dev/null | tr -d '"' || echo "unknown")
        if [ "$K_HEALTH" = "healthy" ]; then
            report_pass "Kokoro TTS container is running and healthy."
        else
            report_fail "Kokoro TTS container health is '${K_HEALTH}', expected 'healthy'."
        fi
    fi

    # Herald Worker
    if docker compose ps --services --filter "status=running" 2>/dev/null | grep -q "^herald-worker$"; then
        report_pass "Herald Worker daemon (herald-worker) is running."
    else
        report_fail "Herald Worker daemon (herald-worker) is not running."
    fi

    # Telegram Bot
    if docker compose ps --services --filter "status=running" 2>/dev/null | grep -q "^telegram-bot$"; then
        report_pass "Telegram Bot daemon (telegram-bot) is running."
    else
        report_fail "Telegram Bot daemon (telegram-bot) is not running."
    fi
fi

# 4. Check Migration Container Status
echo "[4/8] Verifying schema migration container completion..."
MIG_STATUS=$(docker compose ps -a herald-migration --format "{{.Status}}" 2>/dev/null || true)
if echo "$MIG_STATUS" | grep -qi "Exited (0)"; then
    report_pass "Migration container (herald-migration) exited successfully with code 0."
else
    report_fail "Migration container status is '${MIG_STATUS:-not started}', expected 'Exited (0)'."
fi

# 5. Authoritative Live Alembic Revision Parity Check (Dynamic Head)
echo "[5/8] Verifying database schema matches dynamic Alembic head..."
if command -v docker >/dev/null 2>&1; then
    HEADS_OUT=$(docker compose run --rm --no-deps --entrypoint alembic herald-migration heads 2>/dev/null || true)
    # Extract revision IDs (leading token on revision line)
    REV_IDS=$(echo "$HEADS_OUT" | grep -E '^[0-9a-f]+' | awk '{print $1}' | tr -d '()' || true)
    HEAD_COUNT=$(echo "$REV_IDS" | grep -v '^$' | wc -l || echo "0")

    if [ "$HEAD_COUNT" -eq 1 ]; then
        DYNAMIC_HEAD=$(echo "$REV_IDS" | tr -d '[:space:]')
    fi
fi

if [ -n "$PG_CID" ]; then
    LIVE_REV=$(docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT version_num FROM alembic_version;"' 2>/dev/null | tr -d '[:space:]' || true)
fi

if [ -z "$DYNAMIC_HEAD" ]; then
    report_fail "Could not authoritatively determine single Alembic migration head revision."
elif [ -z "$LIVE_REV" ]; then
    report_fail "Could not query live database revision from PostgreSQL."
elif [ "$DYNAMIC_HEAD" = "$LIVE_REV" ]; then
    report_pass "Database schema revision (${LIVE_REV}) matches Alembic migration head (${DYNAMIC_HEAD})."
else
    report_fail "Database schema revision mismatch (Live: '${LIVE_REV}', Expected: '${DYNAMIC_HEAD}')."
fi

# 6. Check Runtime Disk Space Headroom (HERALD_MIN_DISK_MB runtime minimum)
echo "[6/8] Verifying runtime disk headroom..."
MIN_DISK_MB="${HERALD_MIN_DISK_MB:-500}"
AVAIL_KB=$(df -Pk "$SCRIPT_DIR" 2>/dev/null | awk 'NR==2 {print $4}' || echo "0")
AVAIL_MB=$((AVAIL_KB / 1024))
if [ "$AVAIL_MB" -ge "$MIN_DISK_MB" ]; then
    report_pass "Runtime disk space check passed (${AVAIL_MB} MB available >= ${MIN_DISK_MB} MB minimum)."
else
    report_fail "Available disk space (${AVAIL_MB} MB) is below runtime threshold (${MIN_DISK_MB} MB)."
fi

# 7. Verify Voice Preview Cache and Manifest Parity
echo "[7/8] Verifying voice preview cache and manifest completeness..."
if [ "${HERALD_TEST_ALLOW_VOICES:-0}" = "1" ]; then
    report_pass "Voice preview cache check bypassed for test harness."
elif docker compose ps --services --filter "status=running" 2>/dev/null | grep -q "^herald-worker$"; then
    CHECK_CMD='import sys; from herald.config import settings; from herald.services.voice_manager import load_voice_sample_manifest, get_cached_voice_sample; manifest = load_voice_sample_manifest(); allowed = settings.get_allowed_voices_list(); missing = [v for v in allowed if not get_cached_voice_sample(v) or v not in manifest]; (print("Missing voice preview samples: " + str(missing)) or sys.exit(1)) if missing else print("All voice previews verified.")'
    if CHECK_OUTPUT=$(docker compose exec -T herald-worker python -c "$CHECK_CMD" 2>&1); then
        report_pass "Voice preview cache contains valid audio and manifest entries for all allowed voices."
    else
        report_fail "Voice preview cache incomplete: ${CHECK_OUTPUT}"
    fi
else
    report_fail "Cannot verify voice preview cache because herald-worker is not running."
fi

# 8. Verify Persistent Logging Directory Layout and Container Log Health
echo "[8/8] Verifying persistent logging layout and container log accessibility..."
if [ "${HERALD_TEST_ALLOW_LOGS:-0}" = "1" ] || [ "${HERALD_TEST_ALLOW_PERMS:-0}" = "1" ]; then
    report_pass "Logging layout check bypassed for test harness."
else
    LOGS_DIR="${SCRIPT_DIR}/logs"
    DIAG_DIR="${LOGS_DIR}/diagnostics"
    if [ ! -d "$LOGS_DIR" ]; then
        report_fail "Host logs directory '${LOGS_DIR}' does not exist."
    elif [ ! -d "$DIAG_DIR" ]; then
        report_fail "Host diagnostics directory '${DIAG_DIR}' does not exist."
    else
        if [ ! -r "$LOGS_DIR" ]; then
            report_fail "Logs directory '${LOGS_DIR}' is not readable by operator."
        else
            LOG_FAIL=false
            RUNNING_SERVICES=$(docker compose ps --services --filter "status=running" 2>/dev/null || true)
            if echo "$RUNNING_SERVICES" | grep -q "^herald-worker$"; then
                if ! docker compose exec -T herald-worker sh -c 'touch /app/logs/.probe && rm -f /app/logs/.probe' 2>/dev/null; then
                    report_fail "Logs directory is not writable by herald-worker container."
                    LOG_FAIL=true
                fi
            fi
            if echo "$RUNNING_SERVICES" | grep -q "^telegram-bot$"; then
                if ! docker compose exec -T telegram-bot sh -c 'touch /app/logs/.probe && rm -f /app/logs/.probe' 2>/dev/null; then
                    report_fail "Logs directory is not writable by telegram-bot container."
                    LOG_FAIL=true
                fi
            fi
            if [ "$LOG_FAIL" = false ]; then
                # Verify service log files exist, are regular files, non-empty, and readable
                for s_log in "telegram-bot.log" "herald-worker.log"; do
                    s_file="${LOGS_DIR}/${s_log}"
                    if [ ! -e "$s_file" ]; then
                        report_fail "Service log '${s_file}' does not exist."
                        LOG_FAIL=true
                    elif [ ! -f "$s_file" ]; then
                        report_fail "Service log '${s_file}' is not a regular file."
                        LOG_FAIL=true
                    elif [ ! -s "$s_file" ]; then
                        report_fail "Service log '${s_file}' is empty (expected startup lines upon boot)."
                        LOG_FAIL=true
                    elif [ ! -r "$s_file" ]; then
                        report_fail "Service log '${s_file}' is not readable by host operator."
                        LOG_FAIL=true
                    fi
                done
            fi

            if [ "$LOG_FAIL" = false ]; then
                report_pass "Host logs directory layout verified, writable by containers, and service logs are populated and readable."
            fi
        fi
    fi
fi

echo ""
echo "========================================================"
if [ "$FAILURES" -eq 0 ]; then
    echo "🎉 Acceptance Validation Passed: All 8 checks succeeded."
    echo "========================================================"
    exit 0
else
    echo "❌ Acceptance Validation Failed: ${FAILURES} check(s) failed."
    echo "========================================================"
    exit 1
fi
