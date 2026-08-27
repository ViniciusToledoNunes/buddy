from __future__ import annotations

import json
import shutil
import subprocess

import numpy as np

from .base import ThreadedAudioCapture


def pipewire_command(speaker: str, config) -> list[str]:
    binary = shutil.which("pw-record") or shutil.which("pw-cat")
    if not binary:
        raise RuntimeError("PipeWire capture tool not found (install pipewire-audio-client-libraries)")
    configured = config.microphone_device if speaker == "ME" else config.system_device
    # pw-cat (pw-record is a symlink to it) has no --raw option: it writes raw PCM
    # whenever the destination is "-", and rejects the unknown flag outright.
    command = [
        binary, "--record", f"--rate={config.sample_rate}", "--channels=1", "--format=f32",
        f"--latency={config.chunk_ms}ms",
    ]
    if configured != "default":
        command.append(f"--target={configured}")
    if speaker == "REMOTE":
        command.append('--properties={"stream.capture.sink":true}')
    command.append("-")
    return command


class LinuxAudioCapture(ThreadedAudioCapture):
    """PipeWire native stream capture through pw-record/pw-cat."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.process: subprocess.Popen[bytes] | None = None

    def _stderr_tail(self) -> str:
        """Why the child died. pw-record reports rejected options only on stderr, so
        without this the retry loop repeats a generic end-of-stream message forever."""
        process = self.process
        if process is None or process.stderr is None:
            return "no detail"
        if process.poll() is None:
            return "no output from a running stream"
        message = process.stderr.read().decode("utf-8", "replace").strip()
        return message.splitlines()[0] if message else "no detail"

    def _run(self) -> None:
        frames = max(1, int(self.config.sample_rate * self.config.chunk_ms / 1000))
        byte_count = frames * np.dtype("<f4").itemsize
        sequence = 0
        while not self.stop_event.is_set():
            try:
                self.process = subprocess.Popen(pipewire_command(self.speaker, self.config), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self._status("connected", "PipeWire")
                assert self.process.stdout is not None
                while not self.stop_event.is_set():
                    data = self.process.stdout.read(byte_count)
                    if len(data) != byte_count:
                        raise RuntimeError(f"PipeWire stream ended: {self._stderr_tail()}")
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
                self.process = None
        self._status("stopped")


def _pipewire_nodes() -> list[dict[str, object]]:
    binary = shutil.which("pw-dump")
    if not binary:
        return []
    result = subprocess.run([binary], capture_output=True, text=True, timeout=5, check=True)
    nodes: list[dict[str, object]] = []
    for item in json.loads(result.stdout):
        if "Node" not in str(item.get("type", "")):
            continue
        props = item.get("info", {}).get("props", {})
        media_class = str(props.get("media.class", ""))
        if not media_class.startswith("Audio/"):
            continue
        nodes.append({
            "name": props.get("node.description") or props.get("node.nick") or props.get("node.name"),
            "id": props.get("node.name") or item.get("id"), "media_class": media_class,
        })
    return nodes


def list_devices() -> dict[str, object]:
    nodes = _pipewire_nodes()
    return {
        "backend": "pipewire", "default_speaker": "PipeWire default sink",
        "default_microphone": "PipeWire default source",
        "speakers": [x for x in nodes if x["media_class"] == "Audio/Sink"],
        "microphones": [{**x, "loopback": False} for x in nodes if x["media_class"] == "Audio/Source"],
    }


def capture_probe(speaker: str, config, seconds: float = 0.6) -> tuple[bool, str]:
    """Run the real capture command briefly. A binary that exists on PATH proves
    nothing: the command it is invoked with still has to be one PipeWire accepts."""
    try:
        command = pipewire_command(speaker, config)
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
    binary = shutil.which("pw-record") or shutil.which("pw-cat")
    if not binary:
        return [{"name": "PipeWire audio", "state": "failed", "detail": "pw-record/pw-cat not found"}]
    try:
        context = f"{binary}; {len(_pipewire_nodes())} audio node(s) visible"
    except Exception as exc:
        return [{"name": "PipeWire audio", "state": "warning", "detail": str(exc)}]
    checks = []
    for name, speaker in (("System audio (PipeWire)", "REMOTE"), ("Microphone (PipeWire)", "ME")):
        working, detail = capture_probe(speaker, config)
        checks.append({
            "name": name,
            "state": "ok" if working else "failed",
            "detail": f"{context}; {detail}",
        })
    return checks
