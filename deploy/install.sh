#!/usr/bin/env bash
# GravityClaw VPS Installer
# Run on a fresh VPS:
#   curl -sSL https://raw.githubusercontent.com/AhmadSid110/gravityclaw/main/deploy/install.sh | bash
#
# Or clone and run locally:
#   git clone https://github.com/AhmadSid110/gravityclaw.git
#   cd gravityclaw && ./deploy/install.sh
#
# Environment overrides:
#   GRAVITYCLAW_INSTALL_DIR  - install root (default: ~/.local/lib/gravityclaw)
#   GRAVITYCLAW_BRANCH       - git branch to install from (default: main)
#   SKIP_DOCKER              - set to 1 to skip Docker/Podman install
#   SKIP_NODE                - set to 1 to skip Node.js install (skips frontend build)
#   SKIP_SERVICE             - set to 1 to skip systemd service setup

set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
INSTALL_DIR="${GRAVITYCLAW_INSTALL_DIR:-${HOME}/.local/lib/gravityclaw}"
BRANCH="${GRAVITYCLAW_BRANCH:-main}"
REPO_URL="https://github.com/AhmadSid110/gravityclaw.git"
MIN_PYTHON="3.12"
PYTHON="${PYTHON:-python3}"

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

# ─── Pre-flight checks ───────────────────────────────────────────────────────
info "GravityClaw VPS Installer"
echo "─────────────────────────────────────────────────────────────────"

# Check OS
if [[ "$(uname -s)" != "Linux" ]]; then
    fail "This installer supports Linux only."
fi

# Check Python version
check_python() {
    local version
    if ! command -v "${PYTHON}" &>/dev/null; then
        return 1
    fi
    version=$("${PYTHON}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    if [[ "$(printf '%s\n' "${MIN_PYTHON}" "${version}" | sort -V | head -1)" == "${MIN_PYTHON}" ]]; then
        return 0
    fi
    return 1
}

# ─── Install system dependencies ─────────────────────────────────────────────
install_system_deps() {
    info "Checking system dependencies..."

    if command -v apt-get &>/dev/null; then
        local pkgs=()
        command -v git &>/dev/null || pkgs+=(git)
        command -v curl &>/dev/null || pkgs+=(curl)
        check_python || pkgs+=(python3 python3-venv python3-pip)

        if [[ ${#pkgs[@]} -gt 0 ]]; then
            info "Installing: ${pkgs[*]}"
            sudo apt-get update -qq
            sudo apt-get install -y -qq "${pkgs[@]}"
        fi
    elif command -v dnf &>/dev/null; then
        local pkgs=()
        command -v git &>/dev/null || pkgs+=(git)
        command -v curl &>/dev/null || pkgs+=(curl)
        check_python || pkgs+=(python3 python3-pip)

        if [[ ${#pkgs[@]} -gt 0 ]]; then
            info "Installing: ${pkgs[*]}"
            sudo dnf install -y -q "${pkgs[@]}"
        fi
    else
        warn "Unsupported package manager. Ensure git, curl, and python3.12+ are installed."
    fi

    check_python || fail "Python ${MIN_PYTHON}+ is required but not found."
    ok "Python: $("${PYTHON}" --version)"
}

# ─── Install Podman (rootless) ────────────────────────────────────────────────
install_podman() {
    if [[ "${SKIP_DOCKER:-}" == "1" ]]; then
        warn "Skipping container runtime install (SKIP_DOCKER=1)"
        return
    fi

    if command -v podman &>/dev/null; then
        ok "Podman: $(podman --version)"
        return
    fi

    info "Installing Podman (rootless container runtime)..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y -qq podman slirp4netns uidmap
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y -q podman slirp4netns
    else
        warn "Cannot install Podman automatically. Install it manually."
        return
    fi

    # Enable rootless mode
    if [[ "$(id -u)" -ne 0 ]]; then
        systemctl --user enable --now podman.socket 2>/dev/null || true
    fi
    ok "Podman: $(podman --version)"
}

# ─── Install Node.js (for frontend build) ────────────────────────────────────
install_node() {
    if [[ "${SKIP_NODE:-}" == "1" ]]; then
        warn "Skipping Node.js install (SKIP_NODE=1); frontend won't be built"
        return 1
    fi

    if command -v node &>/dev/null; then
        ok "Node.js: $(node --version)"
        return 0
    fi

    info "Installing Node.js 22 LTS..."
    if command -v apt-get &>/dev/null; then
        curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - 2>/dev/null
        sudo apt-get install -y -qq nodejs
    elif command -v dnf &>/dev/null; then
        curl -fsSL https://rpm.nodesource.com/setup_22.x | sudo bash - 2>/dev/null
        sudo dnf install -y -q nodejs
    else
        warn "Cannot install Node.js automatically. Install it manually for the web console."
        return 1
    fi
    ok "Node.js: $(node --version)"
    return 0
}

# ─── Clone or update source ──────────────────────────────────────────────────
get_source() {
    local source_dir="${INSTALL_DIR}/source"

    if [[ -d "${source_dir}/.git" ]]; then
        info "Updating existing source..."
        git -C "${source_dir}" fetch origin "${BRANCH}" --quiet
        git -C "${source_dir}" checkout "${BRANCH}" --quiet
        git -C "${source_dir}" pull origin "${BRANCH}" --quiet
    else
        info "Cloning GravityClaw (branch: ${BRANCH})..."
        mkdir -p "${INSTALL_DIR}"
        git clone --branch "${BRANCH}" --depth 1 "${REPO_URL}" "${source_dir}"
    fi

    ok "Source: ${source_dir}"
    echo "${source_dir}"
}

# ─── Build frontend ──────────────────────────────────────────────────────────
build_frontend() {
    local source_dir="$1"

    if [[ -f "${source_dir}/web/dist/index.html" ]]; then
        ok "Frontend already built"
        return
    fi

    if ! command -v node &>/dev/null; then
        warn "Node.js not available; skipping frontend build"
        warn "The web console will not be served. Install Node.js and re-run to enable it."
        return
    fi

    info "Building web console..."
    cd "${source_dir}/web"
    npm ci --ignore-scripts 2>/dev/null || npm install
    npm run build
    ok "Frontend built: ${source_dir}/web/dist/"
}

# ─── Install Python package ──────────────────────────────────────────────────
install_python_package() {
    local source_dir="$1"
    local venv_dir="${INSTALL_DIR}/venv"

    info "Creating Python virtual environment..."
    "${PYTHON}" -m venv "${venv_dir}"
    "${venv_dir}/bin/python" -m pip install --upgrade pip --quiet
    "${venv_dir}/bin/python" -m pip install "${source_dir}" --quiet
    ok "Package installed in: ${venv_dir}"
}

# ─── Build worker image ──────────────────────────────────────────────────────
build_worker_image() {
    local source_dir="$1"

    if [[ "${SKIP_DOCKER:-}" == "1" ]] || ! command -v podman &>/dev/null; then
        warn "Skipping worker image build (Podman not available)"
        return
    fi

    if podman image inspect localhost/gravityclaw-agy:1.1.13 &>/dev/null; then
        ok "Worker image already exists"
        return
    fi

    # Check if .tools/antigravity exists (needed for the worker image)
    if [[ ! -f "${source_dir}/.tools/antigravity" ]]; then
        warn "AGY binary not found at ${source_dir}/.tools/antigravity"
        warn "Worker image will be built after you place the binary and run:"
        warn "  gravityclaw worker build --source ${source_dir}"
        return
    fi

    info "Building worker container image..."
    podman build -f "${source_dir}/worker/Containerfile.agy" \
        -t localhost/gravityclaw-agy:1.1.13 "${source_dir}"
    ok "Worker image: localhost/gravityclaw-agy:1.1.13"
}

# ─── Setup GravityClaw ────────────────────────────────────────────────────────
setup_gravityclaw() {
    local venv_dir="${INSTALL_DIR}/venv"
    local gc="${venv_dir}/bin/gravityclaw"

    info "Running GravityClaw setup..."
    "${gc}" setup
    ok "GravityClaw setup complete"
}

# ─── Symlink to PATH ─────────────────────────────────────────────────────────
add_to_path() {
    local venv_dir="${INSTALL_DIR}/venv"
    local bin_dir="${HOME}/.local/bin"

    mkdir -p "${bin_dir}"
    ln -sf "${venv_dir}/bin/gravityclaw" "${bin_dir}/gravityclaw"
    ln -sf "${venv_dir}/bin/gravityclaw-server" "${bin_dir}/gravityclaw-server"

    if [[ ":${PATH}:" != *":${bin_dir}:"* ]]; then
        warn "Add to your shell profile: export PATH=\"\${HOME}/.local/bin:\${PATH}\""
    fi
    ok "Symlinked to: ${bin_dir}/gravityclaw"
}

# ─── Main ─────────────────────────────────────────────────────────────────────
main() {
    echo
    install_system_deps
    install_podman
    install_node

    local source_dir
    source_dir=$(get_source)

    build_frontend "${source_dir}"
    install_python_package "${source_dir}"
    build_worker_image "${source_dir}"
    setup_gravityclaw
    add_to_path

    echo
    echo "─────────────────────────────────────────────────────────────────"
    echo -e "${GREEN}GravityClaw installed successfully!${NC}"
    echo
    echo "Next steps:"
    echo "  1. Authenticate AGY (the reasoning engine):"
    echo "     See: https://ai.google.dev/antigravity/docs/install (official install & auth guide)"
    echo
    echo "  2. Verify installation:"
    echo "     gravityclaw doctor"
    echo
    echo "  3. Start the service:"
    echo "     gravityclaw start"
    echo "     # or: systemctl --user start gravityclaw"
    echo
    echo "  4. Access the console:"
    echo "     http://localhost:8787"
    echo
    echo "  5. (Optional) Enable HTTPS with Caddy:"
    echo "     docker compose --profile with-proxy up -d"
    echo
    echo "Configuration: ~/.config/gravityclaw/gravityclaw.toml"
    echo "Data:          ~/.local/share/gravityclaw/"
    echo "Logs:          ~/.local/state/gravityclaw/logs/"
    echo "─────────────────────────────────────────────────────────────────"
}

main "$@"
