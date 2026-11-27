"""Buddy as an app you double-click: a small macOS bundle that runs `buddy open`.

The bundle holds no Python: its executable is a shell script that starts the user's
login shell, so Buddy gets the same PATH and variables as from a terminal (the brain
runs `claude`, which a Finder launch would not find), and runs `buddy open` from this
checkout. Rebuild it with `buddy app` after moving the checkout.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import project_root

BUNDLE_ID = "dev.buddy.meeting-copilot"
ICON_PNG = project_root() / "native" / "macos" / "Buddy.png"
DEFAULT_APP = Path.home() / "Applications" / "Buddy.app"

LAUNCHER = """#!/bin/sh
# Buddy.app: open Buddy's window, starting Buddy first when it is off. Built by `buddy app`.
LOG="$HOME/Library/Logs/Buddy.log"
mkdir -p "$(dirname "$LOG")"
SHELL_BIN="${{SHELL:-/bin/zsh}}"
[ -x "$SHELL_BIN" ] || SHELL_BIN=/bin/zsh
# A login, interactive shell: the same PATH and variables as a terminal.
exec "$SHELL_BIN" -l -i -c '{buddy} open' >>"$LOG" 2>&1 </dev/null
"""


def info_plist(version: str = "1.0") -> dict:
    return {
        "CFBundleName": "Buddy",
        "CFBundleDisplayName": "Buddy",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "buddy-launcher",
        "CFBundleIconFile": "Buddy",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "13.0",
        # The launcher exits at once; the window is its own process.
        "LSUIElement": True,
    }


def _icns(png: Path, out: Path) -> bool:
    """Make an .icns from a 1024 px PNG with the system's own tools; False when they are missing."""
    if not png.exists() or shutil.which("sips") is None or shutil.which("iconutil") is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Buddy.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                subprocess.run(["sips", "-z", str(size * scale), str(size * scale), str(png), "--out", str(iconset / name)],
                               check=True, capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True, capture_output=True)
    return True


def build_app(buddy: Path, dest: Path = DEFAULT_APP, icon_png: Path = ICON_PNG) -> Path:
    """Write Buddy.app at `dest`, replacing an older one. Returns the bundle path."""
    if dest.exists():
        shutil.rmtree(dest)
    contents = dest / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(info_plist(), handle)
    launcher = contents / "MacOS" / "buddy-launcher"
    launcher.write_text(LAUNCHER.format(buddy=_quoted(str(buddy))), encoding="utf-8")
    launcher.chmod(0o755)
    _icns(icon_png, contents / "Resources" / "Buddy.icns")
    # Finder caches icons by path: touching the bundle makes it look again.
    os.utime(dest)
    return dest


def _quoted(path: str) -> str:
    """The path inside the single-quoted -c script, double-quoted so spaces survive."""
    if "'" in path:
        raise ValueError(f"Cannot build the app for a path with a single quote: {path}")
    return f'"{path}"'
