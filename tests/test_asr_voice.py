import asyncio

import numpy as np

from meeting_agent.asr.local import run_local_asr
from meeting_agent.audio import AudioFrame
from meeting_agent.config import Settings
from meeting_agent.events import EventBus, SpeechEvent, StatusEvent, TranscriptEvent


class Pool:
    selection = type("Selection", (), {"model": "fake", "compute_type": "int8"})()

    async def load(self):
        return None

    async def transcribe(self, audio):
        return "we should ship on Tuesday", 0.01


async def _run(seconds_of_speech, voice):
    settings = Settings()
    rate, chunk = settings.audio.sample_rate, settings.audio.chunk_ms
    samples = rate * chunk // 1000
    queue = asyncio.Queue()
    clock = 100.0
    frames = [np.zeros(samples, np.float32)] * 12  # calibration window: the room is quiet
    frames += [np.full(samples, 0.2, np.float32)] * int(seconds_of_speech * 1000 / chunk)
    frames += [np.zeros(samples, np.float32)] * 10  # the pause that ends the line
    for index, frame in enumerate(frames):
        queue.put_nowait(AudioFrame("REMOTE", frame, clock + index * chunk / 1000, index))
    bus = EventBus()
    seen = bus.subscribe(maxsize=1000)
    stop = asyncio.Event()
    task = asyncio.create_task(run_local_asr(queue, Pool(), settings, bus, stop, voice))
    await queue.join()
    stop.set()
    await task
    events = []
    while not seen.empty():
        events.append(seen.get_nowait())
    return events


async def test_a_line_carries_its_voice_and_when_it_was_said():
    calls = []

    def voice(audio):
        calls.append(len(audio))
        return np.ones(4) / 2

    events = await _run(3.0, voice)

    lines = [e for e in events if isinstance(e, TranscriptEvent)]
    speech = [e for e in events if isinstance(e, SpeechEvent)]
    assert len(lines) == 1 and np.allclose(lines[0].embedding, 0.5)
    assert lines[0].spoken_from is not None and lines[0].spoken_to > lines[0].spoken_from
    assert calls and calls[0] >= 16_000  # 16 kHz audio, at least the minimum length
    assert speech[0].speaker == "REMOTE" and len(speech) >= 2  # start, then "still talking"


async def test_a_short_line_gets_no_voice_and_a_broken_model_loses_nothing():
    short = await _run(0.6, lambda audio: np.ones(4))
    assert [e.embedding for e in short if isinstance(e, TranscriptEvent)] == [None]

    def broken(audio):
        raise RuntimeError("model file damaged")

    events = await _run(2.0, broken)
    assert [e.text for e in events if isinstance(e, TranscriptEvent)] == ["we should ship on Tuesday"]
    assert any(isinstance(e, StatusEvent) and e.component == "voice" for e in events)


async def test_without_a_voice_nothing_extra_happens():
    events = await _run(3.0, None)

    assert not any(isinstance(e, SpeechEvent) for e in events)
    assert [e.embedding for e in events if isinstance(e, TranscriptEvent)] == [None]
