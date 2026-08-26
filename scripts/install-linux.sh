#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}

"$PYTHON_BIN" -m venv "$REPO_ROOT/.venv"
"$REPO_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$REPO_ROOT/.venv/bin/python" -m pip install -e "$REPO_ROOT[dev]"
"$REPO_ROOT/.venv/bin/python" "$SCRIPT_DIR/install_agent_integrations.py" --project-root "$REPO_ROOT"

if ! command -v pw-record >/dev/null 2>&1 && ! command -v pw-cat >/dev/null 2>&1; then
    echo "PipeWire capture client missing. Install your distribution's pipewire audio client package." >&2
fi
"$REPO_ROOT/.venv/bin/python" -m meeting_agent.cli doctor
echo "Installed. On Wayland, configure a desktop global shortcut if pynput cannot register one."
