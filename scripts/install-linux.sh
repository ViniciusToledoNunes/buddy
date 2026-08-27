#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if (3, 12) <= sys.version_info < (3, 15) else "Buddy requires Python 3.12-3.14")'

# Build prerequisites, checked up front: without them pip fails deep inside a
# compiler log. pynput pulls evdev on Linux, evdev ships no wheels, so the install
# needs CPython headers and a C compiler that the Windows/macOS paths never touch.
PYTHON_TAG=$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
MISSING=""
"$PYTHON_BIN" -c 'import ensurepip' >/dev/null 2>&1 || MISSING="$MISSING python$PYTHON_TAG-venv"
"$PYTHON_BIN" -c 'import os, sysconfig, sys; sys.exit(0 if os.path.exists(os.path.join(sysconfig.get_path("include"), "Python.h")) else 1)' >/dev/null 2>&1 \
    || MISSING="$MISSING python$PYTHON_TAG-dev"
command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 || MISSING="$MISSING build-essential"
if [ -n "$MISSING" ]; then
    echo "Missing build prerequisites:$MISSING" >&2
    echo "On Debian/Ubuntu run: sudo apt install$MISSING" >&2
    exit 1
fi

"$PYTHON_BIN" -m venv "$REPO_ROOT/.venv"
"$REPO_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$REPO_ROOT/.venv/bin/python" -m pip install -e "$REPO_ROOT[dev]"
"$REPO_ROOT/.venv/bin/python" "$SCRIPT_DIR/install_agent_integrations.py" --project-root "$REPO_ROOT"

if ! command -v pw-record >/dev/null 2>&1 && ! command -v pw-cat >/dev/null 2>&1; then
    echo "PipeWire capture client missing. Install your distribution's pipewire audio client package." >&2
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "FFmpeg missing: capture and transcription still work, but 'buddy benchmark' cannot read media files." >&2
fi
"$REPO_ROOT/.venv/bin/python" -m meeting_agent.cli doctor
echo "Buddy installed. On Wayland, configure a desktop global shortcut if pynput cannot register one."
