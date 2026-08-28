from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import psutil

from .asr import LocalModelPool, run_local_asr, select_asr
from .audio import AudioCapture, AudioFrame
from .config import Settings, project_root
from .copilot import CopilotWorker, choose_provider
from .events import EventBus, StatusEvent
from .investigator import Investigator
from .memory import MeetingMemoryIndex
from .project import ProjectIndex
from .overlay import SuggestionOverlay, overlay_bridge
from .storage import AudioArchiver, MeetingStorage, audio_archive_worker, storage_worker
from .ui import LiveUI


RUNTIME_DIR = project_root() / ".meeting-agent"
STATE_FILE = RUNTIME_DIR / "state.json"
STOP_FILE = RUNTIME_DIR / "stop.flag"
SUGGEST_FILE = RUNTIME_DIR / "suggest.flag"


def describe_consumer_failures(tasks: list[asyncio.Task], results: list[object]) -> list[str]:
    """Name the consumers that died mid-meeting.

    gather(return_exceptions=True) discards those exceptions, so a copilot killed by
    a single failed snapshot write ended a meeting's suggestions with no visible sign.
    """
    failures: list[str] = []
    for task, result in zip(tasks, results):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            failures.append(f"{task.get_name()}: {type(result).__name__}: {result}")
    return failures


def _hotkey(value: str) -> str:
    names = {"ctrl": "<ctrl>", "alt": "<alt>", "shift": "<shift>", "space": "<space>"}
    return "+".join(names.get(part.strip().lower(), part.strip().lower()) for part in value.split("+"))


def request_stop() -> bool:
    state = read_state()
    if not state or not _state_process_alive(state):
        return False
    RUNTIME_DIR.mkdir(exist_ok=True)
    STOP_FILE.touch()
    return True


def read_state() -> dict[str, object] | None:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _state_process_alive(state: dict[str, object]) -> bool:
    try:
        process = psutil.Process(int(state["pid"]))
        started = datetime.fromisoformat(str(state["started"]).replace("Z", "+00:00"))
        return process.is_running() and abs(process.create_time() - started.timestamp()) < 60
    except (psutil.Error, OSError, ValueError, TypeError, KeyError):
        return False


async def run_session(settings: Settings) -> Path:
    if STATE_FILE.exists():
        state = read_state()
        if state and _state_process_alive(state):
            raise RuntimeError(f"A meeting session is already registered: {state}")
        STATE_FILE.unlink(missing_ok=True)
        STOP_FILE.unlink(missing_ok=True)
        SUGGEST_FILE.unlink(missing_ok=True)
    selection = select_asr(settings)
    bus = EventBus()
    storage = MeetingStorage(settings, {
        "mode": selection.mode,
        "model": selection.model,
        "device": selection.device,
        "compute_type": selection.compute_type,
        "reason": selection.reason,
    })
    provider = choose_provider(settings, local_asr=selection.mode.startswith("local"))
    memory_index = MeetingMemoryIndex(storage.directory.parent, settings.copilot.semantic_memory_max_meetings)
    copilot = CopilotWorker(
        settings,
        provider,
        bus,
        snapshot_path=storage.directory / "copilot.json",
        memory_index=memory_index,
        current_meeting_id=storage.directory.name,
        project_index=ProjectIndex(project_root()),
        investigations_path=storage.directory / "investigations.md",
        investigator=(
            Investigator(settings, project_root(), storage.directory.parent, storage.directory.name)
            if settings.copilot.investigation_enabled and os.getenv("OPENAI_API_KEY")
            else None
        ),
    )
    external_stop = asyncio.Event()
    asr_stop = asyncio.Event()
    consumer_stop = asyncio.Event()
    capture_stop = threading.Event()
    loop = asyncio.get_running_loop()
    max_frames = max(10, settings.audio.queue_seconds * 1000 // settings.audio.chunk_ms)
    mic_queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=max_frames)
    remote_queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=max_frames)
    archive_queue: asyncio.Queue[AudioFrame] | None = (
        asyncio.Queue(maxsize=max_frames * 2) if settings.save_audio else None
    )

    RUNTIME_DIR.mkdir(exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    SUGGEST_FILE.unlink(missing_ok=True)
    STATE_FILE.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started": storage.metadata["started_at"],
                "meeting_dir": str(storage.directory),
                "asr_mode": selection.mode,
                "model": selection.model,
                "recording": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    storage_task = asyncio.create_task(storage_worker(storage, bus, consumer_stop), name="storage")
    copilot_task = asyncio.create_task(copilot.run(consumer_stop), name="copilot")
    ui = LiveUI(settings, bus, str(storage.directory))
    ui_task = asyncio.create_task(ui.run(consumer_stop), name="ui")
    consumer_tasks = [storage_task, copilot_task, ui_task]
    if archive_queue is not None:
        archive = AudioArchiver(storage.directory, settings.audio.sample_rate)
        consumer_tasks.append(
            asyncio.create_task(
                audio_archive_worker(archive, archive_queue, bus, consumer_stop), name="audio-archive"
            )
        )
    if settings.ui.overlay:
        consumer_tasks.append(
            asyncio.create_task(overlay_bridge(SuggestionOverlay(), bus, consumer_stop), name="overlay")
        )
    await asyncio.sleep(0)

    pool = LocalModelPool(selection, settings)
    asr_tasks = [
        asyncio.create_task(run_local_asr(mic_queue, pool, settings, bus, asr_stop), name="asr-me-local"),
        asyncio.create_task(run_local_asr(remote_queue, pool, settings, bus, asr_stop), name="asr-remote-local"),
    ]

    captures = [
        AudioCapture("ME", settings.audio, mic_queue, capture_stop, bus, loop, archive_queue),
        AudioCapture("REMOTE", settings.audio, remote_queue, capture_stop, bus, loop, archive_queue),
    ]
    for capture in captures:
        capture.start()

    def stop_from_hotkey() -> None:
        loop.call_soon_threadsafe(external_stop.set)

    def suggest_from_hotkey() -> None:
        loop.call_soon_threadsafe(copilot.suggest_now)

    # Imported lazily: pynput requires a display server, so a headless host must still record.
    hotkeys = None
    try:
        from pynput import keyboard

        hotkeys = keyboard.GlobalHotKeys(
            {
                _hotkey(settings.hotkeys.toggle_meeting): stop_from_hotkey,
                _hotkey(settings.hotkeys.suggest_now): suggest_from_hotkey,
            }
        )
        hotkeys.start()
        bus.publish(StatusEvent("hotkeys", "connected", f"stop={settings.hotkeys.toggle_meeting}; suggest={settings.hotkeys.suggest_now}"))
    except Exception as exc:
        bus.publish(StatusEvent("hotkeys", "warning", str(exc)))

    async def watch_control_files() -> None:
        while not external_stop.is_set():
            if STOP_FILE.exists():
                external_stop.set()
                return
            if SUGGEST_FILE.exists():
                SUGGEST_FILE.unlink(missing_ok=True)
                copilot.suggest_now()
            await asyncio.sleep(0.2)

    watcher = asyncio.create_task(watch_control_files(), name="control-files")
    try:
        await external_stop.wait()
    except asyncio.CancelledError:
        external_stop.set()
        raise
    finally:
        bus.publish(StatusEvent("session", "stopping", "capture stopping immediately"))
        capture_stop.set()
        for capture in captures:
            await asyncio.to_thread(capture.join)
        # Allow a bounded amount of captured audio to reach a final segment.
        try:
            await asyncio.wait_for(
                asyncio.gather(*(queue.join() for queue in (mic_queue, remote_queue))), timeout=0.5
            )
        except TimeoutError:
            pass
        asr_stop.set()
        await asyncio.gather(*asr_tasks, return_exceptions=True)
        await asyncio.sleep(0.1)
        consumer_stop.set()
        consumer_results = await asyncio.gather(*consumer_tasks, return_exceptions=True)
        failures = describe_consumer_failures(consumer_tasks, consumer_results)
        if failures:
            storage.metadata["consumer_failures"] = failures
            for failure in failures:
                print(f"Buddy subsystem failed during the meeting: {failure}", file=sys.stderr)
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        if hotkeys:
            hotkeys.stop()
        transcript = "\n".join(f"{event.speaker}: {event.text}" for event in storage.final_events)
        summary = await copilot.final_report(transcript)
        await asyncio.to_thread(storage.finish, summary)
        STATE_FILE.unlink(missing_ok=True)
        STOP_FILE.unlink(missing_ok=True)
        SUGGEST_FILE.unlink(missing_ok=True)
    return storage.directory
