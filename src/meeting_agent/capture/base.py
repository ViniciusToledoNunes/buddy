from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Literal

import numpy as np

from ..config import AudioConfig
from ..events import EventBus, StatusEvent

Speaker = Literal["ME", "REMOTE"]


class CaptureStreamLost(RuntimeError):
    """Raised to force the capture retry loop to re-resolve its device."""


class SilenceWatchdog:
    """Detects a capture stream that stays open but stops carrying audio.

    Plugging a headset makes Windows hand the jack to the headset and disable the
    internal endpoint. That endpoint stays valid and keeps returning digital
    silence, so nothing ever raises and the retry loop never re-resolves the
    device. Only the silence itself reveals the failure.
    """

    def __init__(self, timeout_seconds: float = 20.0, floor: float = 1e-5) -> None:
        self.timeout_seconds = timeout_seconds
        self.floor = floor
        self.last_sound = 0.0

    def reset(self, now: float) -> None:
        self.last_sound = now

    def observe(self, samples: np.ndarray, now: float) -> bool:
        """Return True once the stream has carried nothing but silence for too long."""
        if samples.size and float(np.max(np.abs(samples))) > self.floor:
            self.last_sound = now
            return False
        if self.timeout_seconds <= 0:
            return False
        return now - self.last_sound >= self.timeout_seconds



@dataclass(slots=True)
class AudioFrame:
    speaker: Speaker
    samples: np.ndarray
    captured_monotonic: float
    sequence: int


class ThreadedAudioCapture:
    """Common bounded-queue behavior for platform capture backends."""

    def __init__(
        self,
        speaker: Speaker,
        config: AudioConfig,
        queue: asyncio.Queue[AudioFrame],
        stop_event: threading.Event,
        bus: EventBus,
        loop: asyncio.AbstractEventLoop,
        archive_queue: asyncio.Queue[AudioFrame] | None = None,
    ) -> None:
        self.speaker = speaker
        self.config = config
        self.queue = queue
        self.stop_event = stop_event
        self.bus = bus
        self.loop = loop
        self.archive_queue = archive_queue
        self.thread: threading.Thread | None = None
        self.dropped_frames = 0

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=f"capture-{self.speaker}", daemon=True)
        self.thread.start()

    def _enqueue(self, frame: AudioFrame) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped_frames += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped_frames += 1
        if self.archive_queue is not None:
            if self.archive_queue.full():
                try:
                    self.archive_queue.get_nowait()
                    self.archive_queue.task_done()
                except asyncio.QueueEmpty:
                    pass
            try:
                self.archive_queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    def _emit(self, samples: np.ndarray, sequence: int) -> None:
        frame = AudioFrame(self.speaker, np.asarray(samples, dtype=np.float32).reshape(-1), time.monotonic(), sequence)
        self.loop.call_soon_threadsafe(self._enqueue, frame)

    def _status(self, state: str, detail: str = "") -> None:
        self.loop.call_soon_threadsafe(
            self.bus.publish, StatusEvent(f"audio-{self.speaker.lower()}", state, detail)
        )

    def _run(self) -> None:
        raise NotImplementedError

    def join(self, timeout: float = 2.0) -> None:
        if self.thread:
            self.thread.join(timeout)
