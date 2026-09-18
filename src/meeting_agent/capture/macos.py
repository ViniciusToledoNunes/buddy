from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np

from .base import ThreadedAudioCapture


def helper_path() -> str | None:
    bundled = Path(__file__).resolve().parents[3] / ".venv" / "bin" / "meeting-audio-macos"
    for candidate in (
        os.getenv("BUDDY_MACOS_HELPER") or os.getenv("MEETING_AGENT_MACOS_HELPER"),
        shutil.which("meeting-audio-macos"),
        str(bundled),
    ):
        if candidate and Path(candidate).expanduser().exists():
            return str(Path(candidate).expanduser())
    return None


def helper_command(speaker: str, config) -> list[str]:
    helper = helper_path()
    if not helper:
        raise RuntimeError("macOS audio helper not found; run scripts/install-macos.sh")
    configured = config.microphone_device if speaker == "ME" else config.system_device
    return [helper, "--source", "microphone" if speaker == "ME" else "system", "--rate", str(config.sample_rate), "--channels", "1", "--device", configured]


class MacOSAudioCapture(ThreadedAudioCapture):
    """ScreenCaptureKit helper stream (raw little-endian Float32 on stdout)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.process: subprocess.Popen[bytes] | None = None

    def _run(self) -> None:
        frames = max(1, int(self.config.sample_rate * self.config.chunk_ms / 1000))
        byte_count = frames * np.dtype("<f4").itemsize
        sequence = 0
        while not self.stop_event.is_set():
            try:
                self.process = subprocess.Popen(helper_command(self.speaker, self.config), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self._status("connected", "ScreenCaptureKit")
                assert self.process.stdout is not None
                while not self.stop_event.is_set():
                    data = self.process.stdout.read(byte_count)
                    if len(data) != byte_count:
                        raise RuntimeError("macOS helper stream ended")
                    self._emit(np.frombuffer(data, dtype="<f4"), sequence)
                    sequence += 1
            except Exception as exc:
                if not self.stop_event.is_set():
                    self._status("retrying", f"{type(exc).__name__}: {exc}")
                    self.stop_event.wait(1.0)
            finally:
                if self.process and self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                self.process = None
        self._status("stopped")


def list_devices() -> dict[str, object]:
    helper = helper_path()
    if helper:
        try:
            result = subprocess.run([helper, "--list"], capture_output=True, text=True, timeout=5, check=True)
            return json.loads(result.stdout)
        except Exception:
            pass
    return {"backend": "screencapturekit", "default_speaker": "macOS system audio", "default_microphone": "macOS default microphone", "speakers": [], "microphones": []}


def capture_probe(speaker: str, config, seconds: float = 0.6) -> tuple[bool, str]:
    """Run the helper briefly so doctor verifies permissions and real audio output."""
    try:
        command = helper_command(speaker, config)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, RuntimeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    try:
        stdout, stderr = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
    if stdout:
        return True, f"captured {len(stdout)} bytes in {seconds:.1f}s"
    message = stderr.decode("utf-8", "replace").strip().splitlines()
    return False, message[0] if message else "captured no audio data"


def probe(config) -> list[dict[str, str]]:
    helper = helper_path()
    if not helper:
        return [{"name": "ScreenCaptureKit audio", "state": "failed", "detail": "native helper not installed"}]
    checks = []
    for name, speaker in (("System audio (ScreenCaptureKit)", "REMOTE"), ("Microphone (ScreenCaptureKit)", "ME")):
        working, detail = capture_probe(speaker, config)
        checks.append({
            "name": name,
            "state": "ok" if working else "failed",
            "detail": f"{helper}; {detail}",
        })
    return checks
