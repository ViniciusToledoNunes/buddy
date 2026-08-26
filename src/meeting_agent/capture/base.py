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
