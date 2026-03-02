#!/usr/bin/env bash
#
# DLA Uninstaller
# Removes every trace of DLA from this machine — no sudo required.
#
# Usage:
#   bash uninstall.sh
#   bash uninstall.sh --yes              # skip confirmation
#   bash uninstall.sh --keep-data        # remove CLI only, keep config/data
#
# One-liner:
#   curl -sSL https://raw.githubusercontent.com/Raphael-08/dla/master/uninstall.sh | bash

set +e  # don't abort on error — clean up as much as possible

# ── Configuration ─────────────────────────────────────────────────────────────

DLA_HOME="${DLA_HOME:-$HOME/.dla}"
DLA_CONFIG_DIR="${DLA_CONFIG_DIR:-$HOME/config}"

# ── Colors ────────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

info()    { echo -e "${BLUE}ℹ${NC}  $1"; }
ok()      { echo -e "${GREEN}✓${NC}  $1"; }
warn()    { echo -e "${YELLOW}!${NC}  $1"; }
skip()    { echo -e "${CYAN}-${NC}  $1 [skipped]"; }
step()    { echo -e "\n${CYAN}${BOLD}▶  $1${NC}"; }

# ── Args ──────────────────────────────────────────────────────────────────────

YES=false
KEEP_DATA=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --yes|-y)     YES=true; shift ;;
        --keep-data)  KEEP_DATA=true; shift ;;
        --help)
            echo "Usage: uninstall.sh [--yes] [--keep-data]"
            echo ""
            echo "Options:"
            echo "  --yes, -y      Skip confirmation prompt"
            echo "  --keep-data    Remove the DLA service and CLI; keep config, logs, Java, Spark"
            echo "  --help         Show this help"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Banner ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${RED}${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${RED}${BOLD}║              DLA Uninstaller                     ║${NC}"
echo -e "${RED}${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BOLD}This will permanently remove:${NC}"
echo -e "  ${RED}✗${NC}  DLA systemd service   (dla-serve)"
echo -e "  ${RED}✗${NC}  DLA CLI tool          (~/.local/bin/dla)"
if [ "$KEEP_DATA" = false ]; then
echo -e "  ${RED}✗${NC}  DLA home directory    ($DLA_HOME)"
echo -e "  ${RED}✗${NC}  DLA config files      ($DLA_CONFIG_DIR)"
echo -e "  ${RED}✗${NC}  Shell RC lines        (JAVA_HOME, SPARK_HOME added by DLA)"
else
echo -e "  ${CYAN}-${NC}  Config and data kept  (--keep-data)"
fi
echo ""

if [ "$YES" = false ]; then
    read -r -p "Are you sure you want to uninstall DLA? [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi
echo ""

# ── Step 1: Kill running DLA processes ───────────────────────────────────────

step "1/5  Stopping DLA"

if pkill -f "dla serve" 2>/dev/null; then
    ok "Killed running DLA API process"
else
    skip "No running DLA API process"
fi

# ── Step 2: Remove systemd service ───────────────────────────────────────────

step "2/5  Removing systemd service"

systemctl --user stop    dla-serve 2>/dev/null && ok "Service stopped"   || true
systemctl --user disable dla-serve 2>/dev/null && ok "Service disabled"  || true

SERVICE_FILE="$HOME/.config/systemd/user/dla-serve.service"
if [ -f "$SERVICE_FILE" ]; then
    rm -f "$SERVICE_FILE"
    systemctl --user daemon-reload 2>/dev/null || true
    ok "Service file removed: $SERVICE_FILE"
else
    skip "No service file at $SERVICE_FILE"
fi

# ── Step 3: Uninstall DLA CLI ─────────────────────────────────────────────────

step "3/5  Uninstalling DLA CLI"

export PATH="$HOME/.local/bin:$PATH"

if command -v uv &>/dev/null; then
    if uv tool list 2>/dev/null | grep -q "^dla "; then
        uv tool uninstall dla && ok "DLA CLI uninstalled via uv"
    else
        skip "DLA not found in uv tools"
    fi
else
    skip "uv not found"
fi

# Remove any leftover symlink
[ -L "$HOME/.local/bin/dla" ] && rm -f "$HOME/.local/bin/dla" && ok "Removed ~/.local/bin/dla symlink"

# ── Step 4: Remove data directories ──────────────────────────────────────────

if [ "$KEEP_DATA" = false ]; then
    step "4/5  Removing data directories"

    # ~/.dla/ — Java, Spark, logs, config
    if [ -d "$DLA_HOME" ]; then
        rm -rf "$DLA_HOME"
        ok "Removed $DLA_HOME"
    else
        skip "$DLA_HOME not found"
    fi

    # ~/config/ — DLA-specific files only (don't nuke the whole directory)
    if [ -d "$DLA_CONFIG_DIR" ]; then
        for item in config.yaml connections jobs logs; do
            target="$DLA_CONFIG_DIR/$item"
            if [ -e "$target" ]; then
                rm -rf "$target"
                ok "Removed $target"
            fi
        done
        # Remove the dir itself if now empty
        if [ -z "$(ls -A "$DLA_CONFIG_DIR" 2>/dev/null)" ]; then
            rmdir "$DLA_CONFIG_DIR" 2>/dev/null && ok "Removed empty $DLA_CONFIG_DIR"
        fi
    else
        skip "$DLA_CONFIG_DIR not found"
    fi

    # ── Step 5: Clean shell RC ────────────────────────────────────────────────

    step "5/5  Cleaning shell config"

    _clean_rc_file() {
        local rc="$1"
        [ -f "$rc" ] || return 0
        local before after
        before=$(wc -l < "$rc")
        # Remove all DLA-added lines:
        #   • lines containing /.dla/  (JAVA_HOME / SPARK_HOME assignments)
        #   • lines matching "added by DLA installer" comment markers
        #   • orphaned export PATH="$JAVA_HOME/bin:..." and "$SPARK_HOME/bin:..."
        # Also discard the blank line immediately preceding any removed line.
        awk '
            BEGIN { hold = "" }
            {
                is_dla = 0
                if ($0 ~ /[\/.]dla\//)                       is_dla = 1
                if ($0 ~ /added by DLA installer/)           is_dla = 1
                if ($0 ~ /^export PATH="\$JAVA_HOME\/bin/)   is_dla = 1
                if ($0 ~ /^export PATH="\$SPARK_HOME\/bin/)  is_dla = 1

                if (is_dla) { hold = ""; next }

                if ($0 ~ /^[[:space:]]*$/) {
                    if (hold != "") print hold
                    hold = $0
                    next
                }

                if (hold != "") { print hold; hold = "" }
                print
            }
            END { if (hold != "") print hold }
        ' "$rc" > /tmp/_dla_rc_clean && mv /tmp/_dla_rc_clean "$rc"
        after=$(wc -l < "$rc")
        if [ "$before" -ne "$after" ]; then
            ok "Cleaned $rc (removed $((before - after)) line(s))"
        else
            skip "No DLA lines found in $rc"
        fi
    }

    CLEANED_ANY=false
    for _rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
        if [ -f "$_rc" ]; then
            _clean_rc_file "$_rc"
            CLEANED_ANY=true
        fi
    done
    $CLEANED_ANY || skip "No shell RC file found"

else
    step "4/5  Data kept (--keep-data)"
    info "Config, logs, Java, and Spark remain in $DLA_HOME"
    step "5/5  Shell RC kept (--keep-data)"
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║          DLA Uninstall Complete                  ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "DLA has been fully removed from this system."
echo ""
