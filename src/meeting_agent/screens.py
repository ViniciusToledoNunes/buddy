"""Screenshots during a meeting, so the watching session can see what is being shared.

Audio alone misses what people point at: a diagram, a terminal, a document on screen.
While a meeting is recorded, the listener saves one capture of the main display every
interval next to the transcript and names the new ones in the next meeting batch, so the
Claude Code session following Buddy can open them when the talk refers to the screen.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


def capture_screen(path: Path, max_width: int = 1600) -> bool:
    """One silent capture of the main display, scaled down. False when it failed.

    macOS only: `screencapture` needs the Screen Recording permission that Buddy's audio
    capture already asks for. Anywhere else, or without the permission, it just fails.
    """
    if sys.platform != "darwin" or shutil.which("screencapture") is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["screencapture", "-x", "-t", "jpg", str(path)], check=True, capture_output=True, timeout=20)
        if max_width and shutil.which("sips"):
            subprocess.run(["sips", "-Z", str(max_width), str(path)], check=True, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        path.unlink(missing_ok=True)
        return False
    return path.exists() and path.stat().st_size > 0


class ScreenShots:
    """One meeting's captures: when the next one is due, and which ones are not sent yet."""

    def __init__(
        self,
        directory: Path,
        interval: float,
        max_shots: int,
        capture: Callable[[Path], bool],
    ) -> None:
        self.directory = directory
        self.interval = interval
        self.max_shots = max_shots
        self.capture = capture
        self.count = 0
        self.last: float | None = None
        self.pending: list[str] = []
        self._previous_digest = ""

    def due(self, now: float) -> bool:
        if self.count >= self.max_shots:
            return False
        return self.last is None or now - self.last >= self.interval

    def shoot(self, now: float) -> str | None:
        """Take a capture. A screen identical to the previous capture is not kept."""
        self.last = now
        path = self.directory / f"{datetime.now().strftime('%H%M%S')}.jpg"
        if not self.capture(path) or not path.exists():
            return None
        digest = hashlib.sha1(path.read_bytes()).hexdigest()
        if digest == self._previous_digest:
            path.unlink(missing_ok=True)
            return None
        self._previous_digest = digest
        self.count += 1
        self.pending.append(str(path))
        return str(path)

    def take(self) -> list[str]:
        """The captures not yet named in a batch."""
        paths, self.pending = self.pending, []
        return paths
