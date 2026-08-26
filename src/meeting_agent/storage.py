from __future__ import annotations

import asyncio
import json
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings, project_root
from .events import EventBus, StatusEvent, TranscriptEvent
from .audio import AudioFrame
import numpy as np


class MeetingStorage:
    def __init__(self, settings: Settings, selection: dict[str, Any]) -> None:
        root = Path(settings.meetings_dir)
        if not root.is_absolute():
            root = project_root() / root
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.directory = root / stamp
        suffix = 1
        while self.directory.exists():
            self.directory = root / f"{stamp}_{suffix}"
            suffix += 1
        self.directory.mkdir(parents=True)
        self.transcript_txt = self.directory / "transcript.txt"
        self.transcript_jsonl = self.directory / "transcript.jsonl"
        self.summary_md = self.directory / "summary.md"
        self.metadata_json = self.directory / "metadata.json"
        self._final_events: list[TranscriptEvent] = []
        self.metadata: dict[str, Any] = {
            "started_at": datetime.now().astimezone().isoformat(),
            "recording": True,
            "asr": selection,
            "language": settings.language,
            "save_audio": settings.save_audio,
        }
        self._write_metadata()

    @property
    def final_events(self) -> list[TranscriptEvent]:
        return list(self._final_events)

    def _write_metadata(self) -> None:
        temporary = self.metadata_json.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.metadata_json)

    def append(self, event: TranscriptEvent) -> None:
        # Each append is flushed by opening in append mode. A disk failure is caught by
        # the independent storage worker and never reaches capture/ASR.
        with self.transcript_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        if event.final:
            self._final_events.append(event)
            local_time = datetime.fromisoformat(event.timestamp).astimezone().strftime("%H:%M:%S")
            with self.transcript_txt.open("a", encoding="utf-8") as handle:
                handle.write(f"[{local_time}] {event.speaker}: {event.text}\n")

    def finish(self, summary: str) -> None:
        self.summary_md.write_text(summary.rstrip() + "\n", encoding="utf-8")
        self.metadata.update(
            {
                "stopped_at": datetime.now().astimezone().isoformat(),
                "recording": False,
                "final_transcript_events": len(self._final_events),
            }
        )
        self._write_metadata()


async def storage_worker(storage: MeetingStorage, bus: EventBus, stop: asyncio.Event) -> None:
    queue = bus.subscribe(maxsize=2048)
    try:
        while not stop.is_set() or not queue.empty():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            if isinstance(event, TranscriptEvent):
                try:
                    await asyncio.to_thread(storage.append, event)
                except Exception as exc:
                    bus.publish(StatusEvent("storage", "retrying", f"{type(exc).__name__}: {exc}"))
                    await asyncio.sleep(0.2)
                    try:
                        await asyncio.to_thread(storage.append, event)
                    except Exception as retry_exc:
                        bus.publish(StatusEvent("storage", "failed", str(retry_exc)))
    finally:
        bus.unsubscribe(queue)


class AudioArchiver:
    """Optional disk worker; never runs in the capture thread."""

    def __init__(self, directory: Path, sample_rate: int) -> None:
        self.handles: dict[str, wave.Wave_write] = {}
        for speaker, filename in (("ME", "audio_me.wav"), ("REMOTE", "audio_remote.wav")):
            handle = wave.open(str(directory / filename), "wb")
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            self.handles[speaker] = handle

    def write(self, frame: AudioFrame) -> None:
        pcm = (np.clip(frame.samples, -1, 1) * 32767).astype("<i2").tobytes()
        self.handles[frame.speaker].writeframesraw(pcm)

    def close(self) -> None:
        for handle in self.handles.values():
            handle.close()


async def audio_archive_worker(
    archive: AudioArchiver,
    queue: asyncio.Queue[AudioFrame],
    bus: EventBus,
    stop: asyncio.Event,
) -> None:
    try:
        while not stop.is_set() or not queue.empty():
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            try:
                await asyncio.to_thread(archive.write, frame)
            except Exception as exc:
                bus.publish(StatusEvent("audio-storage", "failed", f"{type(exc).__name__}: {exc}"))
            finally:
                queue.task_done()
    finally:
        await asyncio.to_thread(archive.close)
