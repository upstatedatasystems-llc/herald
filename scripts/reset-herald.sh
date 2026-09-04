#!/usr/bin/env bash
set -euo pipefail

# Herald Safe Project Reset Tool
# Scoped strictly to Herald Docker Compose resources

RESET_MODE="" # warm, cold
REMOVE_ENV=false
CONFIRM_YES=false
WARM_FLAG=false
COLD_FLAG=false

usage() {
    cat <<EOF
Herald Reset Tool

Usage:
  scripts/reset-herald.sh --warm [options]
  scripts/reset-herald.sh --cold [options]

Options:
  --warm        Warm reset: drops database/work volumes, stops containers, preserves built images and .env
  --cold        Cold reset: drops database/work volumes, stops containers, removes locally built Herald images, preserves .env
  --remove-env  Explicitly remove the .env configuration file (requires confirmation unless --yes is passed)
  -y, --yes     Automatic non-interactive confirmation (for CI/automated testing)
  -h, --help    Show this help message

Warning:
  Reset is IRREVERSIBLE and will destroy PostgreSQL jobs, pairing state, user settings,
  and work-volume artifacts. Run 'scripts/backup.sh' before reset if you need to retain data.
EOF
    exit 0
}

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --warm)
            WARM_FLAG=true
            shift
            ;;
        --cold)
            COLD_FLAG=true
            shift
            ;;
        --remove-env)
            REMOVE_ENV=true
            shift
            ;;
        -y|--yes)
            CONFIRM_YES=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "❌ Error: Unknown argument '$1'" >&2
            echo "Run 'scripts/reset-herald.sh --help' for usage." >&2
            exit 1
            ;;
    esac
done

if [ "$WARM_FLAG" = true ] && [ "$COLD_FLAG" = true ]; then
    echo "❌ Error: Cannot specify both --warm and --cold mode simultaneously." >&2
    exit 1
elif [ "$WARM_FLAG" = true ]; then
    RESET_MODE="warm"
elif [ "$COLD_FLAG" = true ]; then
    RESET_MODE="cold"
else
    echo "❌ Error: Must specify either --warm or --cold mode." >&2
    echo "Run 'scripts/reset-herald.sh --help' for usage." >&2
    exit 1
fi

echo "========================================================"
echo "          ⚠️   HERALD SYSTEM RESET (${RESET_MODE^^})          "
echo "========================================================"
echo ""
echo "⚠️  WARNING: This will DESTROY Herald application state:"
echo "    - PostgreSQL database jobs, queue history, and pairing state"
echo "    - Telegram owner pairing and user settings"
echo "    - Active diagnostics records and work-volume audio artifacts"
echo "    - Optional n8n state (if n8n profile was used)"
echo ""
echo "Run 'scripts/backup.sh' before proceeding if you need to retain data."
echo "This operation is IRREVERSIBLE."
echo ""

# Interactive confirmation if --yes was not provided
if [ "$CONFIRM_YES" = false ]; then
    if [ -t 0 ] || [ -r /dev/tty ]; then
        INPUT_DEV="/dev/tty"
        if [ -t 0 ]; then INPUT_DEV="/dev/stdin"; fi
        read -r -p "Are you sure you want to perform a ${RESET_MODE} reset? [y/N]: " CONFIRM < "$INPUT_DEV"
        if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
            echo "Operation cancelled. No changes made."
            exit 0
        fi
    else
        echo "❌ Error: Confirmation required in non-interactive environment. Use --yes to confirm." >&2
        exit 1
    fi
fi

# Ensure we are in project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

# 1. Authoritative Image Capture (for COLD reset) before containers stop
BUILT_IMAGE_IDS=""
if [ "$RESET_MODE" = "cold" ] && command -v docker >/dev/null 2>&1; then
    BUILT_IMAGE_IDS=$(docker compose images -q herald-migration herald-worker telegram-bot herald-api 2>/dev/null | sort -u || true)
fi

# 2. Stop Containers and Remove Compose Volumes (Scoped strictly to project)
echo "🛑 Stopping Herald containers and removing project volumes..."
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose down -v --remove-orphans
else
    echo "⚠️  Docker Compose not available or already stopped."
fi

# 3. Handle COLD Reset Image Cleanup
if [ "$RESET_MODE" = "cold" ]; then
    echo "🧹 Removing locally built Herald Docker images..."
    if [ -n "$BUILT_IMAGE_IDS" ]; then
        for img_id in $BUILT_IMAGE_IDS; do
            if [ -n "$img_id" ]; then
                docker rmi "$img_id"
            fi
        done
        echo "✅ Locally built Herald images removed."
    else
        echo "ℹ️  No built Herald images found to remove."
    fi
    echo "ℹ️  Upstream images (postgres, kokoro) preserved."
fi

# 4. Handle .env Configuration File
if [ "$REMOVE_ENV" = true ]; then
    if [ "$CONFIRM_YES" = false ]; then
        echo ""
        echo "⚠️  CRITICAL: You specified --remove-env."
        echo "This will permanently delete your .env file containing Telegram Bot token, AI provider keys, and database passwords."
        INPUT_DEV="/dev/tty"
        if [ -t 0 ]; then INPUT_DEV="/dev/stdin"; fi
        read -r -p "Confirm .env deletion by typing 'yes': " ENV_CONFIRM < "$INPUT_DEV"
        if [ "$ENV_CONFIRM" = "yes" ]; then
            rm -f .env
            echo "🗑️  Configuration file (.env) removed."
        else
            echo "Configuration preserved (.env retained)."
        fi
    else
        rm -f .env
        echo "🗑️  Configuration file (.env) removed."
    fi
else
    echo "✅ Configuration preserved (.env)."
fi

echo ""
echo "========================================================"
echo "✅ Herald ${RESET_MODE} reset complete."
echo "========================================================"
