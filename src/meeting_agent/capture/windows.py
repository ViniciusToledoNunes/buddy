from __future__ import annotations

import warnings

import numpy as np
import soundcard as sc

from .base import ThreadedAudioCapture

warnings.filterwarnings("once", message="data discontinuity in recording")


def pick_device(speaker: str, configured: str):
    if speaker == "REMOTE":
        output = sc.default_speaker() if configured == "default" else next(
            (item for item in sc.all_speakers() if configured.lower() in f"{item.name} {item.id}".lower()), None
        )
        if output is None:
            raise RuntimeError(f"System output device not found: {configured}")
        loopbacks = sc.all_microphones(include_loopback=True)
        device = next((item for item in loopbacks if item.isloopback and item.id == output.id), None)
        if device is None:
            device = next((item for item in loopbacks if item.isloopback and output.name in item.name), None)
        if device is None:
            raise RuntimeError(f"WASAPI loopback unavailable for {output.name}")
        return device
    device = sc.default_microphone() if configured == "default" else next(
        (item for item in sc.all_microphones() if configured.lower() in f"{item.name} {item.id}".lower()), None
    )
    if device is None:
        raise RuntimeError(f"Microphone not found: {configured}")
    return device


class WindowsAudioCapture(ThreadedAudioCapture):
    """Resilient WASAPI loopback/microphone capture."""

    def _run(self) -> None:
        frames = max(1, int(self.config.sample_rate * self.config.chunk_ms / 1000))
        configured = self.config.microphone_device if self.speaker == "ME" else self.config.system_device
        sequence = 0
        while not self.stop_event.is_set():
            try:
                device = pick_device(self.speaker, configured)
                self._status("connected", device.name)
                with device.recorder(
                    samplerate=self.config.sample_rate, channels=1, blocksize=min(frames, 2048)
                ) as recorder:
                    while not self.stop_event.is_set():
                        self._emit(np.asarray(recorder.record(numframes=frames)), sequence)
                        sequence += 1
            except Exception as exc:
                self._status("retrying", f"{type(exc).__name__}: {exc}")
                self.stop_event.wait(1.0)
        self._status("stopped")


def list_devices() -> dict[str, object]:
    speakers = [{"name": x.name, "id": x.id} for x in sc.all_speakers()]
    microphones = [
        {"name": x.name, "id": x.id, "loopback": bool(x.isloopback)}
        for x in sc.all_microphones(include_loopback=True)
    ]
    default_speaker = sc.default_speaker()
    default_microphone = sc.default_microphone()
    return {
        "backend": "wasapi",
        "default_speaker": None if default_speaker is None else default_speaker.name,
        "default_microphone": None if default_microphone is None else default_microphone.name,
        "speakers": speakers,
        "microphones": microphones,
    }


def probe(config) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for speaker, label, configured in (
        ("REMOTE", "System audio (WASAPI)", config.system_device),
        ("ME", "Microphone", config.microphone_device),
    ):
        try:
            device = pick_device(speaker, configured)
            with device.recorder(samplerate=config.sample_rate, channels=1) as recorder:
                recorder.record(numframes=max(240, config.sample_rate // 100))
            results.append({"name": label, "state": "ok", "detail": device.name})
        except Exception as exc:
            results.append({"name": label, "state": "failed", "detail": str(exc)})
    return results
