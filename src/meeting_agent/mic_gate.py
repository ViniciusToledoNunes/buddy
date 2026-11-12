"""Keep your microphone's meeting lines only while the meeting app can hear you.

Most of a meeting you are muted, and the microphone still hears you: talking to someone in
the room, reading aloud, the call itself through the speakers. Those are not part of the
meeting. With `listen.mic_follows_mute`, a small helper (`buddy-mic-state`, macOS) reads the
microphone button of the meeting app through the accessibility tree — "Unmute mic" means
muted in Teams, "Turn on microphone" in Google Meet — and a ME line said while muted is not
kept. Commands to Buddy ("Hey Buddy ...") still work while muted: that is when you would
talk to it. `buddy mic on|off|auto` overrides the app.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

MODES = ("auto", "on", "off")


def helper_path() -> str | None:
    bundled = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "buddy-mic-state"
    for candidate in (os.getenv("BUDDY_MIC_STATE_HELPER"), shutil.which("buddy-mic-state"), str(bundled)):
        if candidate and Path(candidate).expanduser().exists():
            return str(Path(candidate).expanduser())
    return None


def read_mode(path: Path) -> str:
    try:
        mode = json.loads(path.read_text(encoding="utf-8")).get("mode", "auto")
    except (OSError, ValueError, AttributeError):
        return "auto"
    return mode if mode in MODES else "auto"


def write_mode(path: Path, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": mode}), encoding="utf-8")


class MicGate:
    """What the meeting app says about your microphone, and whether a ME line is kept."""

    def __init__(
        self,
        mode_file: Path,
        when_unknown: str = "keep",
        spawn: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        grace_seconds: float = 15.0,
    ) -> None:
        self.mode_file = mode_file
        self.when_unknown = when_unknown
        self.clock = clock
        # A line reaches the listener a few seconds after it was said, so unmuting, saying
        # one sentence and muting again must not lose it: open this recently still counts.
        self.grace_seconds = grace_seconds
        self.last_open = float("-inf")
        self.state = "none"  # "muted", "open" or "none" (no meeting app read)
        self.app: str | None = None
        self.error: str | None = None
        self.dropped = 0
        # The call's participants as the app labels them (Teams), newest list; see speakers.
        self.participant_labels: list[str] = []
        self._spawn = spawn or self._start_helper
        self._process: Any = None
        self._thread: threading.Thread | None = None

    @staticmethod
    def _start_helper() -> Any:
        helper = helper_path()
        if sys.platform != "darwin" or helper is None:
            return None
        return subprocess.Popen(
            [helper, "--interval", "1", "--participants"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
        )

    def start(self) -> bool:
        """Follow the app in the background. False when there is no helper to follow it with."""
        try:
            self._process = self._spawn()
        except OSError as exc:
            self.error = str(exc)
            self._process = None
        if self._process is None or self._process.stdout is None:
            self.error = self.error or "buddy-mic-state is not installed (scripts/install-macos.sh)"
            return False
        self._thread = threading.Thread(target=self._follow, args=(self._process.stdout,), daemon=True)
        self._thread.start()
        return True

    def _follow(self, stream: IO[str]) -> None:
        for line in stream:
            self.update(line)

    def update(self, line: str) -> None:
        try:
            item = json.loads(line)
        except ValueError:
            return
        if isinstance(item.get("participants"), list):
            self.participant_labels = [str(label) for label in item["participants"]]
            return
        state = item.get("state")
        if state in ("muted", "open", "none"):
            if self.state == "open" or state == "open":
                self.last_open = self.clock()
            self.state = state
            self.app = item.get("app")
        if item.get("error"):
            self.error = str(item["error"])

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    @property
    def mode(self) -> str:
        return read_mode(self.mode_file)

    def heard(self) -> bool:
        """Whether a ME line said now belongs to the meeting."""
        mode = self.mode
        if mode != "auto":
            return mode == "on"
        if self.state == "open" or self.clock() - self.last_open <= self.grace_seconds:
            return True
        if self.state == "none":
            return self.when_unknown == "keep"
        return False

    def indicator(self) -> dict[str, Any]:
        """What the window's microphone button shows: kept or not, and why."""
        return {"mode": self.mode, "state": self.state, "app": self.app, "heard": self.heard()}

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "state": self.state, "app": self.app, "dropped": self.dropped, "error": self.error,
                "participants_seen": len(self.participant_labels)}
