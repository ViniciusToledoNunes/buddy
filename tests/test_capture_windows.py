import asyncio
import sys
import threading
import time

import numpy as np
import pytest

if sys.platform != "win32":
    pytest.skip("the WASAPI backend only loads on Windows", allow_module_level=True)

from meeting_agent.capture import windows  # noqa: E402
from meeting_agent.config import AudioConfig  # noqa: E402
from meeting_agent.events import EventBus  # noqa: E402

SILENCE = lambda n: np.zeros(n, dtype=np.float32)  # noqa: E731
SPEECH = lambda n: np.full(n, 0.2, dtype=np.float32)  # noqa: E731


class _FakeRecorder:
    def __init__(self, harness):
        self.harness = harness

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def record(self, numframes):
        time.sleep(0.01)  # pace the fake stream instead of spinning the CPU
        self.harness.frames += 1
        if self.harness.frames > 600:  # safety net: no test may loop forever
            self.harness.stop_event.set()
        return self.harness.produce(numframes)


class _FakeDevice:
    def __init__(self, name, harness):
        self.name = name
        self.id = name
        self.harness = harness

    def recorder(self, **_kwargs):
        return _FakeRecorder(self.harness)


class Harness:
    """Drives WindowsAudioCapture._run against a scripted fake device."""

    def __init__(self, produce, defaults=("Mic A",), connections=2):
        self.produce = produce
        self.defaults = defaults
        self.connections = connections
        self.stop_event = threading.Event()
        self.frames = 0
        self.connects = []
        self.default_reads = 0

    def pick_device(self, speaker, configured):
        self.connects.append(configured)
        if len(self.connects) >= self.connections:
            self.stop_event.set()
        return _FakeDevice(f"Device {len(self.connects)}", self)

    def default_device_id(self, speaker):
        # The first read of each connection is the baseline; later reads report the
        # device Windows would choose now.
        index = min(self.default_reads, len(self.defaults) - 1)
        self.default_reads += 1
        return self.defaults[index]

    async def run(self, config, timeout=20):
        original = windows.pick_device, windows.default_device_id
        windows.pick_device, windows.default_device_id = self.pick_device, self.default_device_id
        try:
            capture = windows.WindowsAudioCapture(
                "ME", config, asyncio.Queue(maxsize=64), self.stop_event, EventBus(), asyncio.get_running_loop()
            )
            await asyncio.wait_for(asyncio.to_thread(capture._run), timeout=timeout)
        finally:
            windows.pick_device, windows.default_device_id = original
        return self.connects


async def test_a_stream_that_goes_silent_re_resolves_the_device():
    """Plugging in a headset leaves the old endpoint valid and returning zeros, so
    only the silence itself can trigger recovery."""
    harness = Harness(SILENCE)

    connects = await harness.run(AudioConfig(chunk_ms=100, silence_reconnect_seconds=0.2))

    assert len(connects) >= 2


async def test_a_changed_default_device_re_resolves_the_device():
    harness = Harness(SPEECH, defaults=("Mic A", "Headset B"))

    connects = await harness.run(AudioConfig(chunk_ms=100, silence_reconnect_seconds=0))

    assert len(connects) >= 2


async def test_a_healthy_default_stream_is_never_reconnected():
    harness = Harness(SPEECH, defaults=("Stable",), connections=99)
    config = AudioConfig(chunk_ms=100, silence_reconnect_seconds=5)
    original = windows.pick_device, windows.default_device_id
    windows.pick_device, windows.default_device_id = harness.pick_device, harness.default_device_id
    try:
        capture = windows.WindowsAudioCapture(
            "ME", config, asyncio.Queue(maxsize=64), harness.stop_event, EventBus(), asyncio.get_running_loop()
        )
        runner = asyncio.create_task(asyncio.to_thread(capture._run))
        await asyncio.sleep(1.0)
        harness.stop_event.set()
        await asyncio.wait_for(runner, timeout=10)
    finally:
        windows.pick_device, windows.default_device_id = original

    assert harness.connects == ["default"]
