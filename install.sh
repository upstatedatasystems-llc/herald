#!/usr/bin/env bash
set -euo pipefail

# Herald Bootstrap Installer for Ubuntu 24.04 LTS
# Product: Herald Telegram-First Podcast Automation System

HERALD_REPO_DEFAULT="https://github.com/upstatedatasystems-llc/herald.git"
HERALD_REPO_URL="${HERALD_REPO_URL:-$HERALD_REPO_DEFAULT}"
HERALD_REF="${HERALD_REF:-main}"
HERALD_INSTALL_DIR="${HERALD_INSTALL_DIR:-$HOME/herald}"
HERALD_MIN_DISK_INSTALL_MB="${HERALD_MIN_DISK_INSTALL_MB:-4000}"
HERALD_WARN_DISK_INSTALL_MB="${HERALD_WARN_DISK_INSTALL_MB:-8000}"

MODE="normal" # normal, update, reinstall
NON_INTERACTIVE=false

usage() {
    cat <<EOF
Herald Bootstrap Installer

Usage:
  install.sh [options]

Options:
  --install-dir <path>  Target directory for installation (default: \$HOME/herald)
  --ref <git-ref>       Git branch, tag, or commit to install (default: main)
  --repo <url>          Git repository URL (default: official Herald repo)
  --update              Update existing Herald installation in-place
  --reinstall           Reinstall on top of existing installation
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
        --non-interactive)
            NON_INTERACTIVE=true
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

echo "========================================================"
echo "          🎙️  Herald — Deployment Installer             "
echo "========================================================"
echo ""

# 1. Non-Root / Operator Safety Check
check_operator_safety() {
    if [ "$(id -u)" -eq 0 ]; then
        echo "❌ Error: Do not run the Herald installer directly as root." >&2
        echo "Please run as a standard user with sudo privileges: e.g. curl ... | bash" >&2
        exit 1
    fi
}

# 2. Operating System Validation (Strictly Ubuntu 24.04 LTS)
check_os() {
    if [ ! -f /etc/os-release ]; then
        echo "❌ Error: Unsupported operating system. Herald requires Ubuntu 24.04 LTS." >&2
        exit 1
    fi

    # Read os-release safely
    local os_id=""
    local os_version=""
    while IFS='=' read -r key val || [ -n "$key" ]; do
        val=$(echo "$val" | tr -d '"' | tr -d "'")
        if [ "$key" = "ID" ]; then os_id="$val"; fi
        if [ "$key" = "VERSION_ID" ]; then os_version="$val"; fi
    done < /etc/os-release

    if [ "$os_id" != "ubuntu" ] || [ "$os_version" != "24.04" ]; then
        echo "❌ Error: Unsupported operating system (${os_id} ${os_version})." >&2
        echo "Herald Phase 2 officially supports Ubuntu 24.04 LTS only." >&2
        exit 1
    fi
    echo "✅ Operating System verified: Ubuntu 24.04 LTS"
}

# 3. Architecture Validation
check_arch() {
    local arch
    arch=$(uname -m)
    case "$arch" in
        x86_64|amd64|aarch64|arm64)
            echo "✅ CPU Architecture verified: ${arch}"
            ;;
        *)
            echo "❌ Error: Unsupported architecture '${arch}'. Herald supports amd64 (x86_64) and arm64 (aarch64)." >&2
            exit 1
            ;;
    esac
}

# 4. Target Directory Validation & Normalization
check_install_dir_safety() {
    # Resolve absolute path
    local target_parent
    target_parent=$(dirname "$HERALD_INSTALL_DIR")
    if [ ! -d "$target_parent" ]; then
        mkdir -p "$target_parent" 2>/dev/null || {
            echo "❌ Error: Cannot create parent directory for '${HERALD_INSTALL_DIR}'." >&2
            exit 1
        }
    fi
    HERALD_INSTALL_DIR="$(cd "$target_parent" && pwd)/$(basename "$HERALD_INSTALL_DIR")"

    # Reject dangerous targets
    local unsafe_dirs=("/" "$HOME" "/etc" "/usr" "/var" "/tmp" "/bin" "/sbin" "/lib" "/boot" "/root")
    for bad_dir in "${unsafe_dirs[@]}"; do
        if [ "$HERALD_INSTALL_DIR" = "$bad_dir" ]; then
            echo "❌ Error: Target installation directory '${HERALD_INSTALL_DIR}' is unsafe." >&2
            exit 1
        fi
    done

    # Validate repo URL scheme
    if [[ ! "$HERALD_REPO_URL" =~ ^https:// ]]; then
        echo "❌ Error: Repository URL must start with 'https://'." >&2
        exit 1
    fi
}

# 5. Disk Space Guard
check_disk_space() {
    local check_path="$HERALD_INSTALL_DIR"
    if [ ! -d "$check_path" ]; then
        check_path="$(dirname "$HERALD_INSTALL_DIR")"
    fi

    local avail_kb
    avail_kb=$(df -Pk "$check_path" | awk 'NR==2 {print $4}')
    local avail_mb=$((avail_kb / 1024))

    if [ "$avail_mb" -lt "$HERALD_MIN_DISK_INSTALL_MB" ]; then
        echo "❌ Error: Insufficient free disk space." >&2
        echo "Available: ${avail_mb} MB. Minimum required: ${HERALD_MIN_DISK_INSTALL_MB} MB." >&2
        exit 1
    fi

    if [ "$avail_mb" -lt "$HERALD_WARN_DISK_INSTALL_MB" ]; then
        echo "⚠️  Warning: Disk space is tight (${avail_mb} MB available, recommended >= ${HERALD_WARN_DISK_INSTALL_MB} MB)."
    else
        echo "✅ Disk space verified: ${avail_mb} MB available"
    fi
}

# 6. Check / Install Prerequisites (git, curl, docker, docker compose)
check_prerequisites() {
    local pkgs_needed=()
    if ! command -v git >/dev/null 2>&1; then pkgs_needed+=("git"); fi
    if ! command -v curl >/dev/null 2>&1; then pkgs_needed+=("curl"); fi

    if [ ${#pkgs_needed[@]} -gt 0 ]; then
        echo "📦 Installing missing prerequisite packages: ${pkgs_needed[*]}..."
        sudo apt-get update -y
        sudo apt-get install -y "${pkgs_needed[@]}"
    fi

    # Check Docker Engine & Compose v2
    local need_docker_install=false
    if ! command -v docker >/dev/null 2>&1; then
        need_docker_install=true
    elif ! docker compose version >/dev/null 2>&1; then
        need_docker_install=true
    fi

    if [ "$need_docker_install" = true ]; then
        echo "🐳 Installing Docker Engine and Docker Compose plugin..."
        sudo install -m 0755 -d /etc/apt/keyrings
        sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
        sudo chmod a+r /etc/apt/keyrings/docker.asc

        local codename
        codename=$(grep VERSION_CODENAME /etc/os-release | cut -d'=' -f2 | tr -d '"')
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" | \
            sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

        sudo apt-get update -y
        sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        sudo systemctl enable --now docker
        sudo usermod -aG docker "$USER"
    fi

    # Ensure daemon is running
    if ! sudo systemctl is-active --quiet docker; then
        echo "⏳ Starting Docker service..."
        sudo systemctl start docker || {
            echo "❌ Error: Could not start Docker daemon." >&2
            exit 1
        }
    fi
}

# 7. Docker Permission Verification and Safe Group Handoff
ensure_docker_access() {
    if docker info >/dev/null 2>&1; then
        echo "✅ Docker Engine & Compose v2 are ready and accessible."
        return 0
    fi

    # Check if user is in docker group
    if id -nG "$USER" | grep -qw "docker" || grep -E "^docker:.*\\b$USER\\b" /etc/group >/dev/null 2>&1; then
        if [ -z "${HERALD_SG_ACTIVE:-}" ]; then
            echo "🔄 Activating docker group session..."
            export HERALD_SG_ACTIVE=1
            export HERALD_INSTALL_DIR HERALD_REF HERALD_REPO_URL MODE NON_INTERACTIVE
            exec sg docker -c "$0 $(printf '%q ' "$@")"
        fi
    fi

    echo "❌ Error: Current user cannot access Docker daemon (permission denied)." >&2
    echo "Please ensure user '$USER' is in the 'docker' group (sudo usermod -aG docker $USER) and run 'newgrp docker' or restart your shell session." >&2
    exit 1
}

# Run host checks
check_operator_safety
check_os
check_arch
check_install_dir_safety
check_disk_space
check_prerequisites
ensure_docker_access

# 8. Dispatch based on Mode: NORMAL / UPDATE / REINSTALL
if [ "$MODE" = "normal" ]; then
    if [ -d "$HERALD_INSTALL_DIR" ] && [ "$(ls -A "$HERALD_INSTALL_DIR" 2>/dev/null)" ]; then
        if [ -f "${HERALD_INSTALL_DIR}/compose.yaml" ] && [ -f "${HERALD_INSTALL_DIR}/setup.sh" ]; then
            echo "❌ Error: Herald installation already exists at '${HERALD_INSTALL_DIR}'." >&2
            echo "To update the existing installation, run: install.sh --update" >&2
            echo "To reinstall, run: install.sh --reinstall" >&2
            exit 1
        else
            echo "❌ Error: Directory '${HERALD_INSTALL_DIR}' exists and is not empty." >&2
            exit 1
        fi
    fi

    echo "📥 Cloning Herald (${HERALD_REF}) into '${HERALD_INSTALL_DIR}'..."
    git clone --depth 1 --branch "$HERALD_REF" "$HERALD_REPO_URL" "$HERALD_INSTALL_DIR"
    cd "$HERALD_INSTALL_DIR"

elif [ "$MODE" = "update" ]; then
    if [ ! -d "$HERALD_INSTALL_DIR" ] || [ ! -d "${HERALD_INSTALL_DIR}/.git" ]; then
        echo "❌ Error: Cannot update. No Git repository found at '${HERALD_INSTALL_DIR}'." >&2
        exit 1
    fi
    cd "$HERALD_INSTALL_DIR"

    # Verify origin
    local_origin=$(git config --get remote.origin.url || true)
    if [[ ! "$local_origin" =~ github.com/upstatedatasystems-llc/herald ]] && [ "$local_origin" != "$HERALD_REPO_URL" ]; then
        echo "❌ Error: Repository origin '${local_origin}' does not match expected Herald origin." >&2
        exit 1
    fi

    # Check for uncommitted changes
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo "❌ Error: Local source tree at '${HERALD_INSTALL_DIR}' has uncommitted changes. Update refused." >&2
        exit 1
    fi

    echo "🔄 Updating Herald repository to ref '${HERALD_REF}'..."
    git fetch origin "$HERALD_REF"
    git checkout "$HERALD_REF"
    git pull --ff-only origin "$HERALD_REF" || true

    echo "🔨 Rebuilding Docker containers..."
    docker compose build

    echo "🚀 Restarting core services..."
    docker compose up -d postgres kokoro herald-migration herald-worker telegram-bot

elif [ "$MODE" = "reinstall" ]; then
    if [ ! -d "$HERALD_INSTALL_DIR" ]; then
        echo "❌ Error: Cannot reinstall. Directory '${HERALD_INSTALL_DIR}' does not exist." >&2
        exit 1
    fi
    cd "$HERALD_INSTALL_DIR"

    if [ -d ".git" ]; then
        echo "🔄 Fetching clean ref '${HERALD_REF}'..."
        git fetch origin "$HERALD_REF" || true
        git checkout "$HERALD_REF" || true
    fi

    echo "🔨 Rebuilding Docker containers..."
    docker compose build
fi

# 9. Execute setup.sh
chmod +x setup.sh scripts/*.sh 2>/dev/null || true

SETUP_ARGS=()
if [ "$NON_INTERACTIVE" = true ]; then
    SETUP_ARGS+=("--non-interactive")
fi

echo "⚙️  Running Herald configuration setup..."
./setup.sh "${SETUP_ARGS[@]}"

# 10. Mandatory Acceptance Gate
echo ""
echo "🔍 Running mandatory installation acceptance validation..."
if [ -f "scripts/install_acceptance.sh" ]; then
    ./scripts/install_acceptance.sh
else
    echo "❌ Error: scripts/install_acceptance.sh not found." >&2
    exit 1
fi

echo ""
echo "========================================================"
echo "🎉 Herald installation and acceptance checks passed!    "
echo "========================================================"
