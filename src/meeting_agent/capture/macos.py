from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import subprocess
import sys
import threading
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
        # ScreenCaptureKit sends frames even when nobody talks, so a helper that sends no
        # bytes at all is dead. That applies to both speakers. Digital silence is only
        # suspicious on the microphone: system audio is silent whenever nobody talks.
        stall = self.config.helper_stall_seconds
        watchdog = SilenceWatchdog(self.config.silence_reconnect_seconds if self.speaker == "ME" else 0.0)
        corrupted_limit = max(1, int(CORRUPTED_RESTART_SECONDS * 1000 / self.config.chunk_ms))
        sequence = 0
        failures = 0
        outage = Outage(self.config.capture_alert_seconds)
        if self.speaker == "ME":
            # Two ScreenCaptureKit streams opened at the same instant interrupt each other
            # (SCStreamError -3805): one helper dies, the other stays alive and silent.
            self.stop_event.wait(MICROPHONE_START_DELAY_SECONDS)
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.process = subprocess.Popen(helper_command(self.speaker, self.config), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self._status("connected", "ScreenCaptureKit")
                assert self.process.stdout is not None
                watchdog.reset(time.monotonic())
                corrupted = 0
                while not self.stop_event.is_set():
                    data = read_frame(self.process.stdout, byte_count, stall or None)
                    now = time.monotonic()
                    if data is None:
                        # After the Mac slept, and after SCStreamError -3805, the helper stayed
                        # alive without sending a byte, so a plain read() blocked forever.
                        raise CaptureStreamLost(f"no audio from the {self.speaker} helper for {stall:g}s")
                    if len(data) != byte_count:
                        raise RuntimeError("macOS helper stream ended")
                    samples, damaged = sanitize(np.frombuffer(data, dtype="<f4"))
                    corrupted = corrupted + 1 if damaged else 0
                    if corrupted >= corrupted_limit:
                        raise CaptureStreamLost(f"the {self.speaker} stream carried corrupted samples for {CORRUPTED_RESTART_SECONDS:g}s")
                    self._emit(samples, sequence)
                    sequence += 1
                    if now - started >= HEALTHY_STREAM_SECONDS:
                        failures = 0
                        if outage.healthy():
                            self._status("recovered", f"the {self.speaker} capture is back")
                    if watchdog.observe(samples, now):
                        raise CaptureStreamLost(f"the {self.speaker} stream carried only digital silence for {self.config.silence_reconnect_seconds:g}s")
            except Exception as exc:
                if not self.stop_event.is_set():
                    reason = f"{type(exc).__name__}: {exc}"
                    error = helper_error(self.process)
                    if error:
                        reason = f"{reason} ({error})"
                    failures += 1
                    delay = retry_delay(failures)
                    if self.speaker == "ME":
                        # Both helpers fail together when the capture service breaks, and
                        # equal backoffs would reopen them at the same instant every time.
                        delay += MICROPHONE_START_DELAY_SECONDS
                    self._status("retrying", reason)
                    print(f"Buddy {self.speaker} capture restarts in {delay:g}s: {reason}", file=sys.stderr, flush=True)
                    if outage.failed(time.monotonic()):
                        detail = f"no {self.speaker} audio for {outage.alert_after:g}s; last error: {reason}."
                        if self.config.restart_capture_service:
                            detail += " " + restart_capture_service()
                        else:
                            detail += f" If it persists, macOS's capture service is likely stuck: run `{RESTART_COMMAND}`."
                        self._status("failing", detail)
                        print(f"Buddy {self.speaker} capture is failing: {detail}", file=sys.stderr, flush=True)
                    self.stop_event.wait(delay)
            finally:
                end_helper(self.process)
                self.process = None
        self._status("stopped")

    def join(self, timeout: float = 2.0) -> None:
        # Ending the helper closes its pipe, which wakes a read blocked on it; without
        # this a stopped listener could leave the helper running after it exited.
        end_helper(self.process)
        super().join(timeout)


# Delay before the microphone helper starts, so both streams are not opened together.
MICROPHONE_START_DELAY_SECONDS = 1.5
# A stream that ran this long without failing resets the retry backoff.
HEALTHY_STREAM_SECONDS = 30.0
# Corrupted samples for this long restart the helper.
CORRUPTED_RESTART_SECONDS = 3.0
# Float32 audio stays within [-1, 1]; anything far outside is a broken buffer.
CORRUPTED_SAMPLE_LIMIT = 8.0


# replayd serves every ScreenCaptureKit client. Once it wedged for days and turned each new
# stream into SCStreamError -3805/-3821 within seconds; restarting the listener did not help,
# killing replayd (launchd starts a fresh one) did.
RESTART_COMMAND = "sudo killall replayd"
# Both speakers fail together, so restart the service at most this often.
SERVICE_RESTART_GAP_SECONDS = 300.0
_service_restart_lock = threading.Lock()
_service_restarted_at: float | None = None


class Outage:
    """Tracks one capture outage: alert once when it lasts, report once when it ends."""

    def __init__(self, alert_after: float) -> None:
        self.alert_after = alert_after
        self.since: float | None = None
        self.announced = False

    def failed(self, now: float) -> bool:
        """Record a failure; True exactly once, when the outage first lasts `alert_after`."""
        if self.since is None:
            self.since = now
        if self.alert_after <= 0 or self.announced or now - self.since < self.alert_after:
            return False
        self.announced = True
        return True

    def healthy(self) -> bool:
        """Record a healthy stream; True when it ends an outage that was announced."""
        announced = self.announced
        self.since = None
        self.announced = False
        return announced


def restart_capture_service() -> str:
    """Kill a stuck replayd without prompting; works only where sudoers allows the command."""
    global _service_restarted_at
    with _service_restart_lock:
        now = time.monotonic()
        if _service_restarted_at is not None and now - _service_restarted_at < SERVICE_RESTART_GAP_SECONDS:
            return "The capture service was restarted moments ago."
        _service_restarted_at = now
    try:
        result = subprocess.run(["sudo", "-n", "/usr/bin/killall", "replayd"], capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return f"Could not restart the capture service ({type(exc).__name__}: {exc}); run `{RESTART_COMMAND}`."
    if result.returncode == 0:
        return "Restarted macOS's capture service (replayd)."
    error = (result.stderr or result.stdout).strip().splitlines()
    return f"Could not restart the capture service ({error[-1] if error else f'exit {result.returncode}'}); run `{RESTART_COMMAND}`."


def retry_delay(failures: int) -> float:
    """1s, 2s, 4s, 8s, then 10s: a helper failing in a loop kept interrupting the other stream."""
    return float(min(10, 2 ** max(0, failures - 1)))


def sanitize(samples: np.ndarray) -> tuple[np.ndarray, bool]:
    """Silence a frame with non-finite or impossible values; return whether it was damaged."""
    if samples.size and (not np.all(np.isfinite(samples)) or float(np.max(np.abs(samples))) > CORRUPTED_SAMPLE_LIMIT):
        return np.zeros_like(samples), True
    return samples, False


def helper_error(process: subprocess.Popen[bytes] | None) -> str:
    """The last line the helper wrote to stderr, once it has exited."""
    if process is None or process.stderr is None or process.poll() is None:
        return ""
    with contextlib.suppress(Exception):
        lines = process.stderr.read(4096).decode(errors="replace").strip().splitlines()
        return lines[-1][:300] if lines else ""
    return ""


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
