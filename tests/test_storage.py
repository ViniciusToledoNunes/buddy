import asyncio
import json
import wave

import numpy as np

from meeting_agent.audio import AudioFrame
from meeting_agent.config import Settings
from meeting_agent.events import EventBus, StatusEvent, TranscriptEvent
from meeting_agent.storage import (
    AudioArchiver,
    MeetingStorage,
    audio_archive_worker,
    storage_worker,
)


def test_incremental_storage(tmp_path):
    settings = Settings(meetings_dir=str(tmp_path))
    storage = MeetingStorage(settings, {"mode": "test"})
    storage.append(TranscriptEvent("REMOTE", "hello", True, "abc"))
    storage.finish("# Summary\n\nDone")
    line = json.loads(storage.transcript_jsonl.read_text(encoding="utf-8"))
    assert line["speaker"] == "REMOTE"
    assert "REMOTE: hello" in storage.transcript_txt.read_text(encoding="utf-8")
    assert storage.summary_md.exists()


def test_optional_audio_archive(tmp_path):
    archive = AudioArchiver(tmp_path, 24000)
    archive.write(AudioFrame("ME", np.zeros(2400, dtype=np.float32), 0.0, 0))
    archive.close()
    with wave.open(str(tmp_path / "audio_me.wav"), "rb") as handle:
        assert handle.getframerate() == 24000
        assert handle.getnframes() == 2400


async def test_storage_worker_persists_transcript_events(tmp_path):
    storage = MeetingStorage(Settings(meetings_dir=str(tmp_path)), {"mode": "test"})
    bus = EventBus()
    stop = asyncio.Event()
    task = asyncio.create_task(storage_worker(storage, bus, stop))
    await asyncio.sleep(0)
    bus.publish(TranscriptEvent("ME", "persisted", True, "u1"))
    await asyncio.sleep(0.1)
    stop.set()
    await task

    assert "persisted" in storage.transcript_txt.read_text(encoding="utf-8")


async def test_storage_worker_reports_failure_without_stopping(tmp_path, monkeypatch):
    storage = MeetingStorage(Settings(meetings_dir=str(tmp_path)), {"mode": "test"})
    monkeypatch.setattr(storage, "append", _raise)
    bus = EventBus()
    output = bus.subscribe()
    stop = asyncio.Event()
    task = asyncio.create_task(storage_worker(storage, bus, stop))
    await asyncio.sleep(0)
    bus.publish(TranscriptEvent("ME", "doomed", True, "u1"))
    await asyncio.sleep(0.5)
    stop.set()
    await task

    states = {event.state for event in _drain(output) if isinstance(event, StatusEvent)}
    assert {"retrying", "failed"} <= states


def _raise(*_args, **_kwargs):
    raise OSError("disk is gone")


def _drain(queue):
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def test_audio_archive_worker_writes_and_closes(tmp_path):
    archive = AudioArchiver(tmp_path, 24000)
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(audio_archive_worker(archive, queue, EventBus(), stop))
    await asyncio.sleep(0)
    await queue.put(AudioFrame("REMOTE", np.zeros(240, dtype=np.float32), 0.0, 0))
    await asyncio.sleep(0.1)
    stop.set()
    await task

    with wave.open(str(tmp_path / "audio_remote.wav"), "rb") as handle:
        assert handle.getnframes() == 240


def test_meeting_directory_never_overwrites_an_existing_meeting(tmp_path):
    settings = Settings(meetings_dir=str(tmp_path))
    first = MeetingStorage(settings, {"mode": "test"})
    second = MeetingStorage(settings, {"mode": "test"})

    assert first.directory != second.directory


def test_metadata_records_stop_and_event_count(tmp_path):
    storage = MeetingStorage(Settings(meetings_dir=str(tmp_path)), {"mode": "test"})
    storage.append(TranscriptEvent("ME", "partial", False, "u1"))
    storage.append(TranscriptEvent("ME", "final", True, "u1"))
    storage.finish("# Summary")

    metadata = json.loads(storage.metadata_json.read_text(encoding="utf-8"))
    assert metadata["final_transcript_events"] == 1
    assert metadata["recording"] is False
    assert len(storage.final_events) == 1
