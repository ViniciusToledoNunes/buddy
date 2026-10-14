from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np

from .base import CaptureStreamLost, SilenceWatchdog, ThreadedAudioCapture


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
        # Only the microphone is watched. System audio is legitimately silent whenever
        # nobody is talking, and reopening it every few seconds would only add gaps.
        timeout = self.config.silence_reconnect_seconds if self.speaker == "ME" else 0.0
        watchdog = SilenceWatchdog(timeout)
        sequence = 0
        while not self.stop_event.is_set():
            try:
                self.process = subprocess.Popen(helper_command(self.speaker, self.config), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self._status("connected", "ScreenCaptureKit")
                assert self.process.stdout is not None
                watchdog.reset(time.monotonic())
                while not self.stop_event.is_set():
                    data = read_frame(self.process.stdout, byte_count, timeout or None)
                    now = time.monotonic()
                    if data is None:
                        # After the Mac slept, the helper stayed alive for days without
                        # sending a byte, so a plain read() blocked forever.
                        raise CaptureStreamLost(f"no audio from the {self.speaker} helper for {timeout:g}s")
                    if len(data) != byte_count:
                        raise RuntimeError("macOS helper stream ended")
                    samples = np.frombuffer(data, dtype="<f4")
                    self._emit(samples, sequence)
                    sequence += 1
                    if watchdog.observe(samples, now):
                        raise CaptureStreamLost(f"the {self.speaker} stream carried only digital silence for {timeout:g}s")
            except Exception as exc:
                if not self.stop_event.is_set():
                    self._status("retrying", f"{type(exc).__name__}: {exc}")
                    self.stop_event.wait(1.0)
            finally:
                end_helper(self.process)
                self.process = None
        self._status("stopped")


def read_frame(stream, byte_count: int, timeout: float | None) -> bytes | None:
    """Read one frame; None when nothing at all arrives for `timeout` seconds."""
    if timeout is None:
        return stream.read(byte_count)
    chunks: list[bytes] = []
    remaining = byte_count
    fd = stream.fileno()
    while remaining:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return None
        chunk = os.read(fd, remaining)
        if not chunk:
            break  # end of stream: the caller sees a short frame
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def end_helper(process: subprocess.Popen[bytes] | None) -> None:
    """Stop a helper and make sure it is gone; a stuck one once outlived its listener by days."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)


def list_devices() -> dict[str, object]:
    helper = helper_path()
    if helper:
        try:
            result = subprocess.run([helper, "--list"], capture_output=True, text=True, timeout=5, check=True)
            return json.loads(result.stdout)
        except Exception:
            pass
    return {"backend": "screencapturekit", "default_speaker": "macOS system audio", "default_microphone": "macOS default microphone", "speakers": [], "microphones": []}


def probe(config) -> list[dict[str, str]]:
    helper = helper_path()
    if not helper:
        return [{"name": "ScreenCaptureKit audio", "state": "failed", "detail": "native helper not installed"}]
    return [{"name": "ScreenCaptureKit audio", "state": "ok", "detail": f"{helper}; Screen Recording and Microphone permissions are required"}]
