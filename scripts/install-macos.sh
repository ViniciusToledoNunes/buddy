#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}

if ! command -v swiftc >/dev/null 2>&1; then
    echo "Xcode Command Line Tools are required (run: xcode-select --install)." >&2
    exit 1
fi

"$PYTHON_BIN" -m venv "$REPO_ROOT/.venv"
"$REPO_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$REPO_ROOT/.venv/bin/python" -m pip install -e "$REPO_ROOT[dev]"
swiftc -O -parse-as-library \
    -framework ScreenCaptureKit -framework CoreMedia -framework AVFoundation \
    "$REPO_ROOT/native/macos/MeetingAudioCapture.swift" \
    -o "$REPO_ROOT/.venv/bin/meeting-audio-macos"
"$REPO_ROOT/.venv/bin/python" "$SCRIPT_DIR/install_agent_integrations.py" --project-root "$REPO_ROOT"
"$REPO_ROOT/.venv/bin/python" -m meeting_agent.cli doctor
echo "Installed. Grant Screen Recording and Microphone permissions when macOS prompts. macOS 15+ is required."
