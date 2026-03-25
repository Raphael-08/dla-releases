#!/usr/bin/env bash
#
# DLA Remote Server Installer
# Same as install.sh but --serve is ON by default.
#
# Usage (via curl — one-liner):
#   curl -sSL https://raw.githubusercontent.com/Raphael-08/dla/master/install-remote.sh | bash
#
# Usage (after SCP):
#   bash /tmp/install-remote.sh [--token TOKEN] [--wheel /tmp/dla.whl] [--no-serve]
#
# NOTE: This file is the server variant of install.sh.
#       Source of truth is install.sh — kept in sync by scripts/release.sh.

set +e  # handle errors explicitly

# ── Configuration ─────────────────────────────────────────────────────────────

DLA_VERSION="${DLA_VERSION:-master}"
DLA_REPO="${DLA_REPO:-Raphael-08/dla}"
DLA_HOME="${DLA_HOME:-$HOME/.dla}"
DLA_PORT="${DLA_PORT:-8420}"
SPARK_VERSION="${SPARK_VERSION:-4.1.1}"
JAVA_VERSION="21"
PYTHON_VERSION="3.12"
ADOPTIUM_BASE="https://github.com/adoptium/temurin21-binaries/releases/download"
ADOPTIUM_VERSION="jdk-21.0.5%2B11"
ADOPTIUM_VERSION_DIR="jdk-21.0.5+11"

# ── Output ────────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

info()    { echo -e "${BLUE}ℹ${NC} $1"; }
success() { echo -e "${GREEN}✓${NC} $1"; }
warn()    { echo -e "${YELLOW}!${NC} $1"; }
error()   { echo -e "${RED}✗${NC} $1"; exit 1; }
step()    { echo -e "\n${CYAN}${BOLD}▶ $1${NC}"; }

# ── Args ──────────────────────────────────────────────────────────────────────

GITHUB_TOKEN=""
SKIP_SPARK=false
SKIP_JAVA=false
SERVE_MODE=true      # ON by default for server installs
WHEEL_PATH=""        # Install from local wheel when provided

while [[ $# -gt 0 ]]; do
    case $1 in
        --token)      GITHUB_TOKEN="$2"; shift 2 ;;
        --skip-spark) SKIP_SPARK=true; shift ;;
        --skip-java)  SKIP_JAVA=true; shift ;;
        --serve)      SERVE_MODE=true; shift ;;
        --no-serve)   SERVE_MODE=false; shift ;;
        --port)       DLA_PORT="$2"; shift 2 ;;
        --repo)       DLA_REPO="$2"; shift 2 ;;
        --version)    DLA_VERSION="$2"; shift 2 ;;
        --wheel)      WHEEL_PATH="$2"; shift 2 ;;
        --help)
            echo "DLA Remote Server Installer"
            echo ""
            echo "Usage: install-remote.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --token TOKEN    GitHub personal access token for private repo"
            echo "  --repo REPO      GitHub repo in user/repo format (default: Raphael-08/dla)"
            echo "  --version VER    Branch, tag, or commit to install (default: master)"
            echo "  --wheel PATH     Install from a local .whl file (offline / bundled mode)"
            echo "  --skip-spark     Skip Spark installation"
            echo "  --skip-java      Skip Java installation"
            echo "  --no-serve       Skip systemd service creation"
            echo "  --port PORT      Port for the DLA API service (default: 8420)"
            echo "  --help           Show this help"
            exit 0
            ;;
        *) error "Unknown option: $1" ;;
    esac
done

# ── Header ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${CYAN}${BOLD}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}${BOLD}║         DLA - Data Lake Automation (Server Install)        ║${NC}"
echo -e "${CYAN}${BOLD}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""

mkdir -p "$DLA_HOME"
info "DLA home: $DLA_HOME"

# ── Platform detection ────────────────────────────────────────────────────────

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
case $ARCH in x86_64|amd64) ARCH="x64" ;; aarch64|arm64) ARCH="aarch64" ;; esac
info "Platform: $OS / $ARCH"

SHELL_RC="$HOME/.bashrc"
[ -f "$HOME/.zshrc" ] && SHELL_RC="$HOME/.zshrc"

add_path() {
    grep -qF "$1" "$SHELL_RC" 2>/dev/null || echo "$1" >> "$SHELL_RC"
}

# ── 1. uv ─────────────────────────────────────────────────────────────────────

step "1/7 Installing uv"
export PATH="$HOME/.local/bin:$PATH"

if command -v uv &>/dev/null; then
    success "uv already installed: $(uv --version)"
else
    curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv_install.sh
    sh /tmp/uv_install.sh </dev/null || true
    rm -f /tmp/uv_install.sh
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv &>/dev/null || error "uv installation failed. Install manually: curl -LsSf https://astral.sh/uv/install.sh | sh"
    success "uv installed"
    add_path 'export PATH="$HOME/.local/bin:$PATH"'
fi

# ── 2. Python ─────────────────────────────────────────────────────────────────

step "2/7 Checking Python $PYTHON_VERSION"
PYTHON_OK=false

for cmd in python3 python; do
    if command -v $cmd &>/dev/null; then
        PY_VER=$($cmd -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
        PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
        if [ "${PY_MINOR:-0}" -ge 12 ]; then
            success "Python $PY_VER found"
            PYTHON_OK=true
            break
        fi
    fi
done

if [ "$PYTHON_OK" = false ]; then
    info "Installing Python $PYTHON_VERSION via uv..."
    uv python install $PYTHON_VERSION || error "Python install failed"
    success "Python $PYTHON_VERSION installed"
fi

# ── 3. Java ───────────────────────────────────────────────────────────────────

step "3/7 Checking Java $JAVA_VERSION"
JAVA_HOME_DIR="$DLA_HOME/java/$ADOPTIUM_VERSION_DIR"

if [ "$SKIP_JAVA" = true ]; then
    warn "Skipping Java (--skip-java)"
elif command -v java &>/dev/null && java -version 2>&1 | head -1 | grep -q '"21'; then
    success "Java 21 found (system)"
elif [ -d "$JAVA_HOME_DIR" ]; then
    success "Java found at $JAVA_HOME_DIR"
    export JAVA_HOME="$JAVA_HOME_DIR"
    export PATH="$JAVA_HOME/bin:$PATH"
else
    info "Downloading Adoptium Temurin JDK $JAVA_VERSION..."
    mkdir -p "$DLA_HOME/java"

    if [ "$OS" = "linux" ]; then
        JDK_URL="${ADOPTIUM_BASE}/${ADOPTIUM_VERSION}/OpenJDK21U-jdk_${ARCH}_linux_hotspot_21.0.5_11.tar.gz"
    elif [ "$OS" = "darwin" ]; then
        JDK_URL="${ADOPTIUM_BASE}/${ADOPTIUM_VERSION}/OpenJDK21U-jdk_${ARCH}_mac_hotspot_21.0.5_11.tar.gz"
    else
        error "Unsupported OS: $OS"
    fi

    curl -L --progress-bar -o /tmp/java.tar.gz "$JDK_URL" </dev/null
    tar -xzf /tmp/java.tar.gz -C "$DLA_HOME/java"
    rm -f /tmp/java.tar.gz

    if [ "$OS" = "darwin" ] && [ -d "$JAVA_HOME_DIR/Contents/Home" ]; then
        mv "$JAVA_HOME_DIR" "${JAVA_HOME_DIR}_tmp"
        mv "${JAVA_HOME_DIR}_tmp/Contents/Home" "$JAVA_HOME_DIR"
        rm -rf "${JAVA_HOME_DIR}_tmp"
    fi

    export JAVA_HOME="$JAVA_HOME_DIR"
    export PATH="$JAVA_HOME/bin:$PATH"
    success "Java installed to $JAVA_HOME_DIR"
    add_path "export JAVA_HOME=\"$JAVA_HOME_DIR\""
    add_path 'export PATH="$JAVA_HOME/bin:$PATH"'
fi

# ── 4. Spark ──────────────────────────────────────────────────────────────────

step "4/7 Checking Spark $SPARK_VERSION"
SPARK_DIR="$DLA_HOME/spark/spark-${SPARK_VERSION}-bin-hadoop3"

if [ "$SKIP_SPARK" = true ]; then
    warn "Skipping Spark (--skip-spark)"
elif [ -d "$SPARK_DIR" ]; then
    success "Spark found at $SPARK_DIR"
    export SPARK_HOME="$SPARK_DIR"
    export PATH="$SPARK_HOME/bin:$PATH"
elif [ -n "$SPARK_HOME" ] && [ -d "$SPARK_HOME" ]; then
    success "SPARK_HOME already set: $SPARK_HOME"
else
    info "Downloading Spark $SPARK_VERSION..."
    mkdir -p "$DLA_HOME/spark"
    SPARK_URL="https://dlcdn.apache.org/spark/spark-${SPARK_VERSION}/spark-${SPARK_VERSION}-bin-hadoop3.tgz"

    curl -L --progress-bar -o /tmp/spark.tgz "$SPARK_URL" </dev/null || {
        warn "Primary mirror failed, trying archive..."
        curl -L --progress-bar -o /tmp/spark.tgz \
            "https://archive.apache.org/dist/spark/spark-${SPARK_VERSION}/spark-${SPARK_VERSION}-bin-hadoop3.tgz" </dev/null
    }

    tar -xzf /tmp/spark.tgz -C "$DLA_HOME/spark"
    rm -f /tmp/spark.tgz

    export SPARK_HOME="$SPARK_DIR"
    export PATH="$SPARK_HOME/bin:$PATH"
    success "Spark installed to $SPARK_DIR"
    add_path "export SPARK_HOME=\"$SPARK_DIR\""
    add_path 'export PATH="$SPARK_HOME/bin:$PATH"'
fi

# ── 5. DLA ────────────────────────────────────────────────────────────────────

step "5/7 Installing DLA"
export PATH="$HOME/.local/bin:$PATH"

if [ -n "$WHEEL_PATH" ]; then
    info "Installing from local wheel: $WHEEL_PATH"
    uv tool install "$WHEEL_PATH" --force
elif [ -n "$GITHUB_TOKEN" ]; then
    info "Installing from private repository..."
    uv tool install "git+https://${GITHUB_TOKEN}@github.com/${DLA_REPO}.git@${DLA_VERSION}" --force
else
    SSH_OUT=$(ssh -T git@github.com 2>&1 || true)
    if echo "$SSH_OUT" | grep -q "successfully authenticated"; then
        info "Installing via SSH..."
        uv tool install "git+ssh://git@github.com/${DLA_REPO}.git@${DLA_VERSION}" --force
    else
        info "Installing via HTTPS..."
        uv tool install "git+https://github.com/${DLA_REPO}.git@${DLA_VERSION}" --force
    fi
fi

command -v dla &>/dev/null || { export PATH="$HOME/.local/bin:$PATH"; }
command -v dla &>/dev/null || error "DLA installation failed"
success "DLA installed: $(dla --version 2>&1 | head -1)"

# ── 6. Init + JDBC ────────────────────────────────────────────────────────────

step "6/7 Initializing DLA"
dla init -p "$HOME/config" 2>/dev/null || true
success "Config initialized at $HOME/config"

info "JDBC drivers will be installed automatically when you create a database connection"

# ── 7. Systemd service ────────────────────────────────────────────────────────

if [ "$SERVE_MODE" = true ]; then
    step "7/7 Creating systemd service"

    SERVICE_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SERVICE_DIR"

    cat > "$SERVICE_DIR/dla-serve.service" << UNIT
[Unit]
Description=DLA API Server
After=network.target

[Service]
ExecStart=$HOME/.local/bin/dla serve --port $DLA_PORT
Restart=always
RestartSec=5
Environment=PATH=$HOME/.local/bin:${JAVA_HOME_DIR}/bin:${SPARK_DIR}/bin:/usr/local/bin:/usr/bin:/bin
Environment=JAVA_HOME=${JAVA_HOME_DIR}
Environment=SPARK_HOME=${SPARK_DIR}

[Install]
WantedBy=default.target
UNIT

    loginctl enable-linger "$(whoami)" 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable dla-serve 2>/dev/null || true
    systemctl --user start dla-serve 2>/dev/null || true

    sleep 3

    if systemctl --user is-active dla-serve &>/dev/null; then
        success "DLA API service started on port $DLA_PORT"
    else
        warn "systemd not available — starting manually"
        nohup "$HOME/.local/bin/dla" serve --port "$DLA_PORT" > "$DLA_HOME/serve.log" 2>&1 &
        sleep 2
    fi

    if curl -sf "http://localhost:$DLA_PORT/health" >/dev/null 2>&1; then
        success "DLA API responding on port $DLA_PORT"
    else
        warn "API not responding yet — may still be starting"
    fi
else
    step "7/7 Systemd service skipped (--no-serve)"
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}${BOLD}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║              Installation Complete!                        ║${NC}"
echo -e "${GREEN}${BOLD}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BOLD}Installation Directory:${NC} $DLA_HOME"
echo ""
echo -e "${BOLD}Installed Components:${NC}"
[ -d "$DLA_HOME/java" ] && echo -e "  ${GREEN}✓${NC} Java 21      → $DLA_HOME/java/"
[ -d "$DLA_HOME/spark" ] && echo -e "  ${GREEN}✓${NC} Spark $SPARK_VERSION  → $DLA_HOME/spark/"
[ -d "$HOME/config" ] && echo -e "  ${GREEN}✓${NC} Config       → $HOME/config/"
echo -e "  ${GREEN}✓${NC} DLA CLI      → ~/.local/bin/dla"
[ "$SERVE_MODE" = true ] && echo -e "  ${GREEN}✓${NC} DLA API      → http://localhost:$DLA_PORT"
[ "$SERVE_MODE" = true ] && echo -e "  ${GREEN}✓${NC} Service      → systemctl --user status dla-serve"
echo ""
echo -e "Reload shell: source $SHELL_RC"
echo ""
