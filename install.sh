#!/usr/bin/env bash
set -euo pipefail

# Herald Bootstrap Installer for Ubuntu 24.04 LTS
# Product: Herald Telegram-First Podcast Automation System

ORIGINAL_ARGS=("$@")

HERALD_REPO_DEFAULT="https://github.com/upstatedatasystems-llc/herald.git"
HERALD_REPO_URL="${HERALD_REPO_URL:-$HERALD_REPO_DEFAULT}"
HERALD_REF="${HERALD_REF:-main}"
HERALD_INSTALL_DIR="${HERALD_INSTALL_DIR:-$HOME/herald}"
HERALD_MIN_DISK_INSTALL_MB="${HERALD_MIN_DISK_INSTALL_MB:-4000}"
HERALD_WARN_DISK_INSTALL_MB="${HERALD_WARN_DISK_INSTALL_MB:-8000}"

MODE="normal" # normal, update, reinstall
NON_INTERACTIVE=false
FORCE=false
IS_INTERNAL_DOCKER_STAGE=false
INSTALL_ENV_BACKUP=""
ORIGINAL_STDOUT=3
ORIGINAL_STDERR=4
exec 3>&1 4>&2

cleanup_install_backup() {
    local exit_code=$?
    if [ -n "$INSTALL_ENV_BACKUP" ] && [ -f "$INSTALL_ENV_BACKUP" ]; then
        rm -f "$INSTALL_ENV_BACKUP"
        INSTALL_ENV_BACKUP=""
    fi
    if [ "$exit_code" -ne 0 ]; then
        if [ -n "${HERALD_BOOTSTRAP_LOG:-}" ] && [ -f "${HERALD_BOOTSTRAP_LOG:-}" ]; then
            echo "❌ Installation failed early. Bootstrap transcript preserved at: ${HERALD_BOOTSTRAP_LOG}" >&2
        elif [ -n "${HERALD_INSTALL_LOG:-}" ] && [ -f "${HERALD_INSTALL_LOG:-}" ]; then
            echo "❌ Installation failed. Transcript recorded at: ${HERALD_INSTALL_LOG}" >&2
        fi
    fi
}
trap cleanup_install_backup EXIT INT TERM

usage() {
    cat <<EOF
Herald Bootstrap Installer

Usage:
  install.sh [options]

Options:
  --install-dir <path>  Target directory for installation (default: \$HOME/herald)
  --ref <git-ref>       Git branch, tag, or commit SHA to install (default: main)
  --repo <url>          Git repository URL (default: official Herald repo)
  --update              Update existing Herald installation in-place
  --reinstall           Reinstall on top of existing installation
  --force               Force reinstall even if working tree has untracked/dirty changes
  --non-interactive     Run non-interactively without terminal prompts
  -h, --help            Show this help message

Environment Variables:
  HERALD_INSTALL_DIR, HERALD_REF, HERALD_REPO_URL,
  HERALD_MIN_DISK_INSTALL_MB, HERALD_WARN_DISK_INSTALL_MB
EOF
    exit 0
}

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir)
            HERALD_INSTALL_DIR="$2"
            shift 2
            ;;
        --ref)
            HERALD_REF="$2"
            shift 2
            ;;
        --repo)
            HERALD_REPO_URL="$2"
            shift 2
            ;;
        --update)
            MODE="update"
            shift
            ;;
        --reinstall)
            MODE="reinstall"
            shift
            ;;
        --force)
            FORCE=true
            shift
            ;;
        --non-interactive)
            NON_INTERACTIVE=true
            shift
            ;;
        --internal-docker-stage)
            IS_INTERNAL_DOCKER_STAGE=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "❌ Error: Unknown argument '$1'" >&2
            echo "Run 'install.sh --help' for usage." >&2
            exit 1
            ;;
    esac
done

# Initialize persistent installation transcript immediately
if [ -z "${HERALD_INSTALL_LOG:-}" ]; then
    BOOTSTRAP_LOG=$(mktemp "${TMPDIR:-/tmp}/herald-install-bootstrap-XXXXXX.log" 2>/dev/null || mktemp -t herald-install-bootstrap-XXXXXX.log 2>/dev/null || mktemp /tmp/herald-install-bootstrap-XXXXXX.log)
    chmod 600 "$BOOTSTRAP_LOG" 2>/dev/null || true
    export HERALD_INSTALL_LOG="$BOOTSTRAP_LOG"
    export HERALD_BOOTSTRAP_LOG="$BOOTSTRAP_LOG"
    exec > >(tee -a "$HERALD_INSTALL_LOG") 2> >(tee -a "$HERALD_INSTALL_LOG" >&2)
else
    exec > >(tee -a "$HERALD_INSTALL_LOG") 2> >(tee -a "$HERALD_INSTALL_LOG" >&2)
fi

echo "=== Herald Deployment Installer Invocation ==="
echo "Command arguments: ${ORIGINAL_ARGS[*]:-(none)}"
echo "Mode: ${MODE} | Requested Ref: ${HERALD_REF} | Target Directory: ${HERALD_INSTALL_DIR}"
echo "Repository: ${HERALD_REPO_URL}"
echo ""

if [ "$IS_INTERNAL_DOCKER_STAGE" = false ]; then
    echo "========================================================"
    echo "          🎙️  Herald — Deployment Installer             "
    echo "========================================================"
    echo ""
fi

# 1. Non-Root / Operator Safety Check
check_operator_safety() {
    local uid_val
    uid_val="$(id -u 2>/dev/null || echo "1000")"
    if [ "${HERALD_TEST_ALLOW_ROOT:-0}" != "1" ] && [ "${uid_val:-1000}" -eq 0 ]; then
        echo "❌ Error: Do not run the Herald installer directly as root." >&2
        echo "Please run as a standard user with sudo privileges: e.g. curl ... | bash" >&2
        exit 1
    fi
}

# 2. Operating System Validation (Strictly Ubuntu 24.04 LTS)
check_os() {
    local os_file="${HERALD_TEST_OS_RELEASE:-/etc/os-release}"
    if [ ! -f "$os_file" ]; then
        echo "❌ Error: Unsupported operating system. Herald requires Ubuntu 24.04 LTS." >&2
        exit 1
    fi

    local os_id=""
    local os_version=""
    while IFS='=' read -r key val || [ -n "$key" ]; do
        val=$(echo "$val" | tr -d '"' | tr -d "'")
        if [ "$key" = "ID" ]; then os_id="$val"; fi
        if [ "$key" = "VERSION_ID" ]; then os_version="$val"; fi
    done < "$os_file"

    if [ "$os_id" != "ubuntu" ] || [ "$os_version" != "24.04" ]; then
        echo "❌ Error: Unsupported operating system (${os_id} ${os_version})." >&2
        echo "Herald Phase 2 officially supports Ubuntu 24.04 LTS only." >&2
        exit 1
    fi
    if [ "$IS_INTERNAL_DOCKER_STAGE" = false ]; then
        echo "✅ Operating System verified: Ubuntu 24.04 LTS"
    fi
}

# 3. Architecture Validation
check_arch() {
    local arch="${HERALD_TEST_ARCH:-$(uname -m)}"
    case "$arch" in
        x86_64|amd64|aarch64|arm64)
            if [ "$IS_INTERNAL_DOCKER_STAGE" = false ]; then
                echo "✅ CPU Architecture verified: ${arch}"
            fi
            ;;
        *)
            echo "❌ Error: Unsupported architecture '${arch}'. Herald supports amd64 (x86_64) and arm64 (aarch64)." >&2
            exit 1
            ;;
    esac
}

canonicalize_path() {
    local p="$1"
    if command -v realpath >/dev/null 2>&1; then
        realpath -m "$p" 2>/dev/null || echo "$p"
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import os, sys; sys.stdout.write(os.path.realpath(sys.argv[1]))" "$p" 2>/dev/null || echo "$p"
    else
        echo "$p"
    fi
}

# 4. Target Directory Validation & Canonicalization
check_install_dir_safety() {
    local raw_target="$HERALD_INSTALL_DIR"
    raw_target="${raw_target%/}"
    if [ -z "$raw_target" ]; then raw_target="/"; fi

    local canon_target
    canon_target=$(canonicalize_path "$raw_target")
    canon_target="${canon_target%/}"
    if [ -z "$canon_target" ]; then canon_target="/"; fi

    local canon_home
    canon_home=$(canonicalize_path "$HOME")
    canon_home="${canon_home%/}"

    local unsafe_dirs=("/" "$HOME" "$canon_home" "/etc" "/usr" "/var" "/tmp" "/bin" "/sbin" "/lib" "/lib64" "/boot" "/root" "/sys" "/proc" "/dev")
    for bad_dir in "${unsafe_dirs[@]}"; do
        if [ "$raw_target" = "$bad_dir" ] || [ "$canon_target" = "$bad_dir" ]; then
            echo "❌ Error: Target installation directory '${raw_target}' is unsafe." >&2
            exit 1
        fi
    done

    # Reject . or .. or empty base
    if [ "$raw_target" = "." ] || [ "$raw_target" = ".." ]; then
        echo "❌ Error: Target installation directory '${raw_target}' is invalid." >&2
        exit 1
    fi
    local base_name
    base_name=$(basename "$canon_target")
    if [ "$base_name" = "." ] || [ "$base_name" = ".." ] || [ -z "$base_name" ]; then
        echo "❌ Error: Target installation directory '${raw_target}' is invalid." >&2
        exit 1
    fi

    HERALD_INSTALL_DIR="$canon_target"

    if [ "${HERALD_TEST_ALLOW_FILE_REPO:-0}" != "1" ] && [[ ! "$HERALD_REPO_URL" =~ ^https:// ]]; then
        echo "❌ Error: Repository URL must start with 'https://'." >&2
        exit 1
    fi
}

# 5. Disk Space Guard (4000 MB hard fail, 8000 MB warn)
check_disk_space() {
    local avail_mb="${HERALD_TEST_AVAIL_MB:-}"
    if [ -z "$avail_mb" ]; then
        local check_path="$HERALD_INSTALL_DIR"
        if [ ! -d "$check_path" ]; then
            check_path="$(dirname "$HERALD_INSTALL_DIR")"
        fi
        local avail_kb
        avail_kb=$(df -Pk "$check_path" 2>/dev/null | awk 'NR==2 {print $4}' || echo "0")
        avail_mb=$((avail_kb / 1024))
    fi

    if [ "$avail_mb" -lt "$HERALD_MIN_DISK_INSTALL_MB" ]; then
        echo "❌ Error: Insufficient free disk space." >&2
        echo "Available: ${avail_mb} MB. Minimum required: ${HERALD_MIN_DISK_INSTALL_MB} MB." >&2
        exit 1
    fi

    if [ "$avail_mb" -lt "$HERALD_WARN_DISK_INSTALL_MB" ]; then
        echo "⚠️  Warning: Disk space is tight (${avail_mb} MB available, recommended >= ${HERALD_WARN_DISK_INSTALL_MB} MB)."
    elif [ "$IS_INTERNAL_DOCKER_STAGE" = false ]; then
        echo "✅ Disk space verified: ${avail_mb} MB available"
    fi
}

# 6. Check / Install Prerequisites (git, curl, python3)
check_prerequisites() {
    local pkgs_needed=()
    if ! command -v git >/dev/null 2>&1; then pkgs_needed+=("git"); fi
    if ! command -v curl >/dev/null 2>&1; then pkgs_needed+=("curl"); fi
    if ! command -v python3 >/dev/null 2>&1; then pkgs_needed+=("python3"); fi

    if [ ${#pkgs_needed[@]} -gt 0 ]; then
        echo "📦 Installing missing prerequisite packages: ${pkgs_needed[*]}..."
        if command -v sudo >/dev/null 2>&1; then
            sudo apt-get update -y
            sudo apt-get install -y "${pkgs_needed[@]}"
        else
            echo "❌ Error: sudo is required to install prerequisite packages (${pkgs_needed[*]})." >&2
            exit 1
        fi
    fi
}

normalize_git_url() {
    local u="$1"
    u="${u%.git}"
    u="${u%/}"
    echo "$u"
}

detect_ref_type() {
    local ref="$1"
    if git show-ref --tags --quiet --verify "refs/tags/${ref}" 2>/dev/null || git rev-parse --verify "refs/tags/${ref}^{commit}" >/dev/null 2>&1; then
        echo "tag"
        return
    fi
    if git show-ref --heads --quiet --verify "refs/heads/${ref}" 2>/dev/null || \
       git rev-parse --verify "refs/remotes/origin/${ref}^{commit}" >/dev/null 2>&1 || \
       git rev-parse --verify "refs/heads/${ref}^{commit}" >/dev/null 2>&1; then
        echo "branch"
        return
    fi
    if git rev-parse --verify "${ref}^{commit}" >/dev/null 2>&1; then
        local sha
        sha=$(git rev-parse --verify "${ref}^{commit}" 2>/dev/null || true)
        if [[ "$sha" == "$ref"* ]]; then
            echo "commit"
        else
            echo "branch"
        fi
        return
    fi
    echo "ref"
}

# Run environment & prerequisite validation
check_operator_safety
check_os
check_arch
check_install_dir_safety
check_disk_space
check_prerequisites

# 7. Repository Source Preparation (performed before Docker group handoff so on-disk script is guaranteed)
if [ "$IS_INTERNAL_DOCKER_STAGE" = false ]; then
    if [ "$MODE" = "normal" ]; then
        if [ -d "$HERALD_INSTALL_DIR" ] && [ "$(ls -A "$HERALD_INSTALL_DIR" 2>/dev/null)" ]; then
            if [ -f "${HERALD_INSTALL_DIR}/compose.yaml" ] && [ -f "${HERALD_INSTALL_DIR}/setup.sh" ]; then
                echo "❌ Error: Herald installation already exists at '${HERALD_INSTALL_DIR}'." >&2
                echo "To update the existing installation, run: ./install.sh --update" >&2
                echo "To reinstall, run: ./install.sh --reinstall" >&2
                exit 1
            else
                echo "❌ Error: Directory '${HERALD_INSTALL_DIR}' exists and is not empty." >&2
                exit 1
            fi
        fi

        echo "📥 Cloning Herald repository into '${HERALD_INSTALL_DIR}'..."
        git clone "$HERALD_REPO_URL" "$HERALD_INSTALL_DIR"
        cd "$HERALD_INSTALL_DIR"

        echo "🔄 Checking out ref '${HERALD_REF}'..."
        git checkout "$HERALD_REF"
        REF_TYPE=$(detect_ref_type "$HERALD_REF")
        INSTALLED_SHA=$(git rev-parse HEAD)
        echo "📌 Requested ref: ${HERALD_REF} (type: ${REF_TYPE}, resolved commit: ${INSTALLED_SHA})"
        echo "✅ Checked out commit ${INSTALLED_SHA}"

    elif [ "$MODE" = "update" ]; then
        if [ ! -d "$HERALD_INSTALL_DIR" ] || [ ! -d "${HERALD_INSTALL_DIR}/.git" ]; then
            echo "❌ Error: Cannot update. No Git repository found at '${HERALD_INSTALL_DIR}'." >&2
            exit 1
        fi
        cd "$HERALD_INSTALL_DIR"

        # Verify origin URL matches expected Herald repo
        local_origin=$(git config --get remote.origin.url || true)
        norm_local=$(normalize_git_url "$local_origin")
        norm_expected=$(normalize_git_url "$HERALD_REPO_URL")
        norm_default=$(normalize_git_url "$HERALD_REPO_DEFAULT")

        if [ "$norm_local" != "$norm_expected" ] && [ "$norm_local" != "$norm_default" ]; then
            echo "❌ Error: Repository origin '${local_origin}' does not match expected Herald origin." >&2
            exit 1
        fi

        # Check for uncommitted and untracked changes
        if [ -n "$(git status --porcelain)" ]; then
            echo "❌ Error: Local source tree at '${HERALD_INSTALL_DIR}' has uncommitted or untracked changes. Update refused." >&2
            exit 1
        fi

        echo "🔄 Fetching ref '${HERALD_REF}'..."
        git fetch origin "$HERALD_REF"
        git checkout "$HERALD_REF"
        git pull --ff-only origin "$HERALD_REF"
        REF_TYPE=$(detect_ref_type "$HERALD_REF")
        INSTALLED_SHA=$(git rev-parse HEAD)
        echo "📌 Requested ref: ${HERALD_REF} (type: ${REF_TYPE}, resolved commit: ${INSTALLED_SHA})"
        echo "✅ Updated to commit ${INSTALLED_SHA}"

    elif [ "$MODE" = "reinstall" ]; then
        if [ ! -d "$HERALD_INSTALL_DIR" ] || [ ! -d "${HERALD_INSTALL_DIR}/.git" ]; then
            echo "❌ Error: Cannot reinstall. No Git repository found at '${HERALD_INSTALL_DIR}'." >&2
            exit 1
        fi
        cd "$HERALD_INSTALL_DIR"

        local_origin=$(git config --get remote.origin.url || true)
        norm_local=$(normalize_git_url "$local_origin")
        norm_expected=$(normalize_git_url "$HERALD_REPO_URL")
        norm_default=$(normalize_git_url "$HERALD_REPO_DEFAULT")

        if [ "$norm_local" != "$norm_expected" ] && [ "$norm_local" != "$norm_default" ]; then
            echo "❌ Error: Repository origin '${local_origin}' does not match expected Herald origin." >&2
            exit 1
        fi

        if [ -n "$(git status --porcelain)" ] && [ "$FORCE" = false ]; then
            echo "❌ Error: Local source tree has uncommitted or untracked changes. Use --force to proceed with reinstall." >&2
            exit 1
        fi

        echo "🔄 Fetching requested ref '${HERALD_REF}'..."
        git fetch origin "$HERALD_REF"

        # Resolve requested ref to exact commit SHA
        RESOLVED_SHA=$(git rev-parse --verify "${HERALD_REF}^{commit}" 2>/dev/null || git rev-parse --verify "origin/${HERALD_REF}^{commit}" 2>/dev/null || true)
        if [ -z "$RESOLVED_SHA" ]; then
            echo "❌ Error: Cannot resolve ref '${HERALD_REF}' to a valid commit SHA." >&2
            exit 1
        fi
        REF_TYPE=$(detect_ref_type "$HERALD_REF")
        echo "📌 Requested ref: ${HERALD_REF} (type: ${REF_TYPE}, resolved commit: ${RESOLVED_SHA})"


        # Backup .env safely before any destructive git clean/reset
        INSTALL_ENV_BACKUP=""
        if [ -f ".env" ]; then
            INSTALL_ENV_BACKUP=$(mktemp)
            chmod 600 "$INSTALL_ENV_BACKUP"
            cp -p ".env" "$INSTALL_ENV_BACKUP"
        fi

        echo "🔄 Restoring repository source to commit ${RESOLVED_SHA}..."
        git checkout "$HERALD_REF" 2>/dev/null || git checkout "$RESOLVED_SHA" 2>/dev/null || true
        git reset --hard "$RESOLVED_SHA"
        git clean -fd -e logs -e logs/*

        # Restore .env if needed and ensure 0600 permissions
        if [ -n "$INSTALL_ENV_BACKUP" ]; then
            if [ ! -f ".env" ]; then
                cp -p "$INSTALL_ENV_BACKUP" ".env"
            fi
            chmod 600 ".env" 2>/dev/null || true
            rm -f "$INSTALL_ENV_BACKUP"
            INSTALL_ENV_BACKUP=""
        fi

        # Verify clean Git working tree (only ignored files like .env and logs should remain)
        if [ -n "$(git status --porcelain)" ]; then
            echo "❌ Error: Working tree is still dirty after reinstall reset." >&2
            exit 1
        fi

        INSTALLED_SHA=$(git rev-parse HEAD)
        echo "✅ Reinstalled clean source at commit ${INSTALLED_SHA}"
    fi
else
    cd "$HERALD_INSTALL_DIR"
fi

# Finalize persistent installation transcript under logs/
mkdir -p "${HERALD_INSTALL_DIR}/logs"
if [ -n "${HERALD_BOOTSTRAP_LOG:-}" ] && [ -f "${HERALD_BOOTSTRAP_LOG:-}" ]; then
    TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)
    FINAL_INSTALL_LOG="${HERALD_INSTALL_DIR}/logs/install-${TIMESTAMP}.log"
    cp -p "$HERALD_BOOTSTRAP_LOG" "$FINAL_INSTALL_LOG" 2>/dev/null || cat "$HERALD_BOOTSTRAP_LOG" > "$FINAL_INSTALL_LOG"
    rm -f "$HERALD_BOOTSTRAP_LOG"
    unset HERALD_BOOTSTRAP_LOG
    chmod 644 "$FINAL_INSTALL_LOG" 2>/dev/null || true
    export HERALD_INSTALL_LOG="$FINAL_INSTALL_LOG"
    echo "📝 Installation transcript finalized at ${HERALD_INSTALL_LOG}"
    exec 1>&3 2>&4
    exec > >(tee -a "$HERALD_INSTALL_LOG") 2> >(tee -a "$HERALD_INSTALL_LOG" >&2)
elif [ -z "${HERALD_INSTALL_LOG:-}" ]; then
    TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)
    FINAL_INSTALL_LOG="${HERALD_INSTALL_DIR}/logs/install-${TIMESTAMP}.log"
    touch "$FINAL_INSTALL_LOG"
    chmod 644 "$FINAL_INSTALL_LOG" 2>/dev/null || true
    export HERALD_INSTALL_LOG="$FINAL_INSTALL_LOG"
    echo "📝 Recording installation transcript to ${HERALD_INSTALL_LOG}"
    exec 1>&3 2>&4
    exec > >(tee -a "$HERALD_INSTALL_LOG") 2> >(tee -a "$HERALD_INSTALL_LOG" >&2)
fi

# 8. Check Docker Engine & Compose v2 Prerequisites
CURRENT_USER="$(id -un)"

ensure_docker_installed() {
    local need_docker=false
    local need_compose=false

    if ! command -v docker >/dev/null 2>&1; then
        need_docker=true
    fi

    if [ "$need_docker" = false ] && ! docker compose version >/dev/null 2>&1; then
        need_compose=true
    fi

    if [ "$need_docker" = true ]; then
        echo "🐳 Installing Docker Engine and Docker Compose plugin..."
        if ! command -v sudo >/dev/null 2>&1; then
            echo "❌ Error: sudo is required to install Docker packages." >&2
            exit 1
        fi
        sudo install -m 0755 -d /etc/apt/keyrings
        sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
        sudo chmod a+r /etc/apt/keyrings/docker.asc

        local codename
        codename=$(grep VERSION_CODENAME /etc/os-release | cut -d'=' -f2 | tr -d '"' || echo "noble")
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" | \
            sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

        sudo apt-get update -y
        sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        sudo systemctl enable --now docker
        sudo usermod -aG docker "$CURRENT_USER"

    elif [ "$need_compose" = true ]; then
        echo "🐳 Installing Docker Compose plugin..."
        if ! command -v sudo >/dev/null 2>&1; then
            echo "❌ Error: sudo is required to install Docker Compose plugin." >&2
            exit 1
        fi
        sudo apt-get update -y
        sudo apt-get install -y docker-compose-plugin
    fi

    if command -v systemctl >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
        if ! sudo systemctl is-active --quiet docker 2>/dev/null; then
            echo "⏳ Starting Docker service..."
            sudo systemctl start docker || {
                echo "❌ Error: Could not start Docker daemon." >&2
                exit 1
            }
        fi
    fi
}

ensure_docker_installed

# 9. Docker Permission Check and Safe Group Handoff
if ! docker info >/dev/null 2>&1; then
    # Check if user is in docker group
    if id -nG "$CURRENT_USER" | grep -qw "docker" || grep -E "^docker:.*\\b$CURRENT_USER\\b" /etc/group >/dev/null 2>&1; then
        if [ -z "${HERALD_SG_ACTIVE:-}" ]; then
            echo "🔄 Activating docker group session..."
            export HERALD_SG_ACTIVE=1
            # Re-execute the ON-DISK script with original arguments
            exec sg docker -c "$(printf '%q ' "$HERALD_INSTALL_DIR/install.sh" --internal-docker-stage "${ORIGINAL_ARGS[@]}")"
        fi
    fi

    echo "❌ Error: Current user '${CURRENT_USER}' cannot access Docker daemon (permission denied)." >&2
    echo "Please run 'sudo usermod -aG docker ${CURRENT_USER}' and activate via 'newgrp docker' or restart your SSH session." >&2
    exit 1
fi

if [ "$IS_INTERNAL_DOCKER_STAGE" = false ]; then
    echo "✅ Docker Engine & Compose v2 are ready and accessible."
fi

# 10. Configuration Setup Phase
chmod +x setup.sh scripts/*.sh 2>/dev/null || true

SETUP_ARGS=()
if [ "$NON_INTERACTIVE" = true ]; then
    SETUP_ARGS+=("--non-interactive")
fi

echo "⚙️  Running Herald configuration setup..."
./setup.sh --configure-only "${SETUP_ARGS[@]}"

# 11. Rebuild Containers (for update/reinstall - strictly after .env is configured)
if [ "$MODE" = "update" ] || [ "$MODE" = "reinstall" ]; then
    echo "🔨 Building Docker service images..."
    docker compose build
fi

# 12. Start Herald Services & Verify Health
echo "🚀 Starting Herald services..."
./setup.sh --start-only --no-banner "${SETUP_ARGS[@]}"

# 13. Mandatory Acceptance Gate
echo ""
echo "🔍 Running mandatory installation acceptance validation..."
if [ -f "scripts/install_acceptance.sh" ]; then
    ./scripts/install_acceptance.sh
else
    echo "❌ Error: scripts/install_acceptance.sh not found." >&2
    exit 1
fi

# Retrieve bot username and pairing details for final completion banner
BOT_NAME="HeraldBot"
if [ -f ".env" ]; then
    TG_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' .env 2>/dev/null | cut -d'=' -f2- | tr -d '"' | tr -d "'" || true)
    if [ -n "$TG_TOKEN" ]; then
        TG_ME_RESP=$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$TG_TOKEN" | curl -s --config - 2>/dev/null || true)
        BOT_NAME=$(echo "$TG_ME_RESP" | grep -o '"username":"[^"]*' | cut -d'"' -f4 || echo "HeraldBot")
    fi
fi
BOT_NAME="${BOT_NAME:-HeraldBot}"

RAW_PAIRING_OUTPUT=""
PAIRING_OUTPUT=""
if RAW_PAIRING_OUTPUT=$(docker compose exec -T telegram-bot python -m herald.telegram.pairing_cli --read-only 2>&1); then
    PAIRING_OUTPUT=$(echo "$RAW_PAIRING_OUTPUT" | tr -d '\r' | awk 'NR==1{print $0}')
fi

PAIRING_MODE="UNKNOWN"
PAIR_CODE=""
PAIR_EXP="30"
if [ "$PAIRING_OUTPUT" = "PAIRED" ]; then
    PAIRING_MODE="PAIRED"
elif echo "$PAIRING_OUTPUT" | grep -qE '^UNPAIRED:[A-Za-z0-9_-]+:[0-9]+$'; then
    PAIR_CODE=$(echo "$PAIRING_OUTPUT" | cut -d':' -f2)
    PAIR_EXP=$(echo "$PAIRING_OUTPUT" | cut -d':' -f3)
    PAIRING_MODE="UNPAIRED"
fi

echo ""
echo "========================================================"
echo "               Herald Setup Complete! /logs for details"
echo "========================================================"
echo ""
echo "Telegram Bot: @${BOT_NAME}"

if [ "$PAIRING_MODE" = "PAIRED" ]; then
    echo "Owner:        Owner already paired"
    echo ""
    echo "Your Telegram account is already paired as the authorized owner."
elif [ "$PAIRING_MODE" = "UNPAIRED" ]; then
    echo "Pairing Code: ${PAIR_CODE}"
    echo "Pairing expires in: ${PAIR_EXP:-30} minutes"
    echo ""
    echo "PAIR YOUR ACCOUNT"
    echo "1. Open a private chat with @${BOT_NAME}"
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
