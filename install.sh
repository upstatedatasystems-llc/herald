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

if [ "$IS_INTERNAL_DOCKER_STAGE" = false ]; then
    echo "========================================================"
    echo "          🎙️  Herald — Deployment Installer             "
    echo "========================================================"
    echo ""
fi

# 1. Non-Root / Operator Safety Check
check_operator_safety() {
    if [ "${HERALD_TEST_ALLOW_ROOT:-0}" != "1" ] && [ "$(id -u)" -eq 0 ]; then
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

# 4. Target Directory Validation & Normalization
check_install_dir_safety() {
    local raw_target="$HERALD_INSTALL_DIR"
    raw_target="${raw_target%/}"
    if [ -z "$raw_target" ]; then raw_target="/"; fi

    local unsafe_dirs=("/" "$HOME" "/etc" "/usr" "/var" "/tmp" "/bin" "/sbin" "/lib" "/boot" "/root")
    for bad_dir in "${unsafe_dirs[@]}"; do
        if [ "$raw_target" = "$bad_dir" ]; then
            echo "❌ Error: Target installation directory '${raw_target}' is unsafe." >&2
            exit 1
        fi
    done

    local target_parent
    target_parent=$(dirname "$HERALD_INSTALL_DIR")
    if [ ! -d "$target_parent" ]; then
        mkdir -p "$target_parent" 2>/dev/null || {
            echo "❌ Error: Cannot create parent directory for '${HERALD_INSTALL_DIR}'." >&2
            exit 1
        }
    fi
    local resolved_parent
    resolved_parent="$(cd "$target_parent" 2>/dev/null && pwd || echo "$target_parent")"
    HERALD_INSTALL_DIR="${resolved_parent%/}/$(basename "$HERALD_INSTALL_DIR")"

    for bad_dir in "${unsafe_dirs[@]}"; do
        if [ "$HERALD_INSTALL_DIR" = "$bad_dir" ]; then
            echo "❌ Error: Target installation directory '${HERALD_INSTALL_DIR}' is unsafe." >&2
            exit 1
        fi
    done

    if [[ ! "$HERALD_REPO_URL" =~ ^https:// ]]; then
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
        INSTALLED_SHA=$(git rev-parse HEAD)
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
        INSTALLED_SHA=$(git rev-parse HEAD)
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

        echo "🔄 Reinstalling clean ref '${HERALD_REF}'..."
        git fetch origin "$HERALD_REF"
        git checkout "$HERALD_REF"
        INSTALLED_SHA=$(git rev-parse HEAD)
        echo "✅ Reinstalled at commit ${INSTALLED_SHA}"
    fi
else
    cd "$HERALD_INSTALL_DIR"
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
            exec sg docker -c "\"$HERALD_INSTALL_DIR/install.sh\" --internal-docker-stage $(printf '%q ' "${ORIGINAL_ARGS[@]}")"
        fi
    fi

    echo "❌ Error: Current user '${CURRENT_USER}' cannot access Docker daemon (permission denied)." >&2
    echo "Please run 'sudo usermod -aG docker ${CURRENT_USER}' and activate via 'newgrp docker' or restart your SSH session." >&2
    exit 1
fi

if [ "$IS_INTERNAL_DOCKER_STAGE" = false ]; then
    echo "✅ Docker Engine & Compose v2 are ready and accessible."
fi

# 10. Rebuild Containers (for update/reinstall)
if [ "$MODE" = "update" ] || [ "$MODE" = "reinstall" ]; then
    echo "🔨 Building Docker service images..."
    docker compose build
fi

# 11. Execute setup.sh
chmod +x setup.sh scripts/*.sh 2>/dev/null || true

SETUP_ARGS=()
if [ "$NON_INTERACTIVE" = true ]; then
    SETUP_ARGS+=("--non-interactive")
fi

echo "⚙️  Running Herald configuration setup..."
./setup.sh "${SETUP_ARGS[@]}"

# 12. Mandatory Acceptance Gate
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
