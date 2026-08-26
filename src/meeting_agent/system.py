from __future__ import annotations

import os
import shutil
from pathlib import Path


def find_ffmpeg(binary: str = "ffmpeg") -> str | None:
    found = shutil.which(binary)
    if found:
        return found
    for candidate in (Path("/opt/homebrew/bin") / binary, Path("/usr/local/bin") / binary):
        if candidate.exists():
            return str(candidate)
    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        return None
    local = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    if local.exists():
        matches = list(local.glob(f"Gyan.FFmpeg*/*/bin/{binary}.exe"))
        if matches:
            return str(sorted(matches, reverse=True)[0])
    return None
