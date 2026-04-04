#!/usr/bin/env bash
# DLA Bootstrap — installs uv + Python, then delegates to installer.py
# This is the only bash you need. Everything else is Python.
set -e
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Run from /tmp to avoid uv picking up a .venv or pyproject.toml in the cwd
cd /tmp
exec uv run --no-project --python 3.12 "$SCRIPT_DIR/installer.py" "$@"
