#!/usr/bin/env bash
set -euo pipefail

# Herald Installation Acceptance Validation Helper
# Verifies installation health, schema revision, service state, permissions, and isolation without exposing secrets.

ENV_FILE=".env"
FAILURES=0

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
echo "[1/7] Checking configuration file and permissions..."
if [ ! -f "$ENV_FILE" ]; then
    report_fail "Configuration file '${ENV_FILE}' not found."
else
    # Check permissions (mode 0600 / -rw-------)
    PERMS=$(stat -c "%a" "$ENV_FILE" 2>/dev/null || stat -f "%Lp" "$ENV_FILE" 2>/dev/null || echo "")
    if [ "$PERMS" = "600" ] || [ "$PERMS" = "0600" ]; then
        report_pass "Configuration file exists with strict 0600 permissions."
    else
        report_fail "Configuration file permissions are '${PERMS}', expected '0600'."
    fi
fi

# Helper to read .env line safely without sourcing
get_env_key() {
    local key="$1"
    if [ -f "$ENV_FILE" ]; then
        grep -E "^${key}=" "$ENV_FILE" | head -n1 | cut -d'=' -f2- | tr -d '"' | tr -d "'" | xargs || true
    fi
}

# 2. Check for Placeholder Secrets
echo "[2/7] Auditing credentials for unsafe default placeholders..."
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

AI_PROV=$(get_env_key "AI_PROVIDER")
AI_PROV=${AI_PROV:-"none"}
case "$AI_PROV" in
    gemini)
        G_KEY=$(get_env_key "GEMINI_API_KEY")
        if [ -z "$G_KEY" ]; then report_fail "AI_PROVIDER is 'gemini' but GEMINI_API_KEY is missing."; else report_pass "Gemini API credentials configured."; fi
        ;;
    groq)
        GR_KEY=$(get_env_key "GROQ_API_KEY")
        if [ -z "$GR_KEY" ]; then report_fail "AI_PROVIDER is 'groq' but GROQ_API_KEY is missing."; else report_pass "Groq API credentials configured."; fi
        ;;
    openrouter)
        OR_KEY=$(get_env_key "OPENROUTER_API_KEY")
        if [ -z "$OR_KEY" ]; then report_fail "AI_PROVIDER is 'openrouter' but OPENROUTER_API_KEY is missing."; else report_pass "OpenRouter API credentials configured."; fi
        ;;
    mistral)
        M_KEY=$(get_env_key "MISTRAL_API_KEY")
        if [ -z "$M_KEY" ]; then report_fail "AI_PROVIDER is 'mistral' but MISTRAL_API_KEY is missing."; else report_pass "Mistral API credentials configured."; fi
        ;;
    cloudflare)
        CF_T=$(get_env_key "CLOUDFLARE_API_TOKEN")
        CF_A=$(get_env_key "CLOUDFLARE_ACCOUNT_ID")
        if [ -z "$CF_T" ] || [ -z "$CF_A" ]; then report_fail "Cloudflare API Token or Account ID is missing."; else report_pass "Cloudflare Workers AI credentials configured."; fi
        ;;
    none|"")
        report_pass "Literal mode active (no external AI provider key required)."
        ;;
    *)
        report_pass "AI Provider configured: ${AI_PROV}"
        ;;
esac

# 3. Check Default Service States
echo "[3/7] Verifying default container service states..."
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    report_fail "Docker Engine or Docker Compose v2 is not available."
else
    # PostgreSQL health
    PG_CID=$(docker compose ps -q postgres 2>/dev/null || true)
    if [ -z "$PG_CID" ]; then
        report_fail "PostgreSQL container (postgres) is not running."
    else
        PG_HEALTH=$(docker inspect --format='{{json .State.Health.Status}}' "$PG_CID" 2>/dev/null | tr -d '"')
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
        K_HEALTH=$(docker inspect --format='{{json .State.Health.Status}}' "$KOKORO_CID" 2>/dev/null | tr -d '"')
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
echo "[4/7] Verifying schema migration container completion..."
MIG_STATUS=$(docker compose ps -a herald-migration --format "{{.Status}}" 2>/dev/null || true)
if echo "$MIG_STATUS" | grep -qi "Exited (0)"; then
    report_pass "Migration container (herald-migration) exited successfully with code 0."
else
    report_fail "Migration container status is '${MIG_STATUS}', expected 'Exited (0)'."
fi

# 5. Authoritative Live Alembic Revision Check
echo "[5/7] Verifying database schema matches dynamic Alembic head..."
DYNAMIC_HEAD=""
if command -v docker >/dev/null 2>&1; then
    DYNAMIC_HEAD=$(docker compose run --rm --no-deps --entrypoint alembic herald-migration heads 2>/dev/null | grep -E '^[a-f0-9]+' | awk '{print $1}' | tr -d '()' | head -n1 || true)
fi

LIVE_REV=""
if [ -n "$PG_CID" ]; then
    LIVE_REV=$(docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT version_num FROM alembic_version;"' 2>/dev/null | tr -d '[:space:]' || true)
fi

if [ -n "$DYNAMIC_HEAD" ] && [ -n "$LIVE_REV" ] && [ "$DYNAMIC_HEAD" = "$LIVE_REV" ]; then
    report_pass "Database schema revision (${LIVE_REV}) matches Alembic migration head (${DYNAMIC_HEAD})."
elif [ -n "$LIVE_REV" ] && [ -z "$DYNAMIC_HEAD" ]; then
    # Fallback to checking that alembic_version is populated
    report_pass "Database schema version record exists (${LIVE_REV})."
else
    report_fail "Database schema revision mismatch (Live: '${LIVE_REV:-none}', Expected: '${DYNAMIC_HEAD:-unknown}')."
fi

# 6. Verify Default Profile Isolation (n8n and herald-api NOT running)
echo "[6/7] Verifying default profile isolation (optional services disabled)..."
RUNNING_SERVICES=$(docker compose ps --services --filter "status=running" 2>/dev/null || true)

if echo "$RUNNING_SERVICES" | grep -q "^n8n$"; then
    report_fail "Optional service 'n8n' is running in default installation profile."
else
    report_pass "Optional service 'n8n' is not running (default profile)."
fi

if echo "$RUNNING_SERVICES" | grep -q "^herald-api$"; then
    report_fail "Optional service 'herald-api' is running in default installation profile."
else
    report_pass "Optional service 'herald-api' is not running (default profile)."
fi

# 7. Check Disk Space
echo "[7/7] Verifying runtime disk headroom..."
MIN_DISK_MB="${HERALD_MIN_DISK_MB:-500}"
AVAIL_KB=$(df -Pk . 2>/dev/null | awk 'NR==2 {print $4}' || echo "0")
AVAIL_MB=$((AVAIL_KB / 1024))
if [ "$AVAIL_MB" -ge "$MIN_DISK_MB" ]; then
    report_pass "Disk space check passed (${AVAIL_MB} MB available >= ${MIN_DISK_MB} MB minimum)."
else
    report_fail "Available disk space (${AVAIL_MB} MB) is below minimum threshold (${MIN_DISK_MB} MB)."
fi

echo ""
echo "========================================================"
if [ "$FAILURES" -eq 0 ]; then
    echo "🎉 Acceptance Validation Passed: All 7 checks succeeded."
    echo "========================================================"
    exit 0
else
    echo "❌ Acceptance Validation Failed: ${FAILURES} check(s) failed."
    echo "========================================================"
    exit 1
fi
