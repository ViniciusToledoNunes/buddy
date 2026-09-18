from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import psutil

from .config import Settings, project_root
from .events import EventBus, TranscriptEvent, utc_now

DEFAULT_RUNTIME_DIR = project_root() / ".meeting-agent"

# "Hey Buddy" opens the utterance. Whisper spells the name a few ways, and "buddy" alone
# is too common in English conversation to be trusted without the "hey".
WAKE = re.compile(
    r"^\W*(?:hey|hi|ok|okay)\W+(?:buddy|buddie|budy|bodie|body)\b\W*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
MEETING_WORD = r"(?:meeting|call|recording)"
START_MEETING = re.compile(
    rf"\b(?:start|starts|starting|begin|begins|beginning)\b.*\b{MEETING_WORD}\b"
    rf"|\b{MEETING_WORD}\b.*\b(?:start|starts|starting|begin|begins|beginning|about to)\b",
    re.IGNORECASE,
)
STOP_MEETING = re.compile(
    rf"\b(?:stop|end|ending|finish|finished|over|done|turn\s+off|switch\s+off)\b.*\b{MEETING_WORD}\b"
    rf"|\b{MEETING_WORD}\b.*\b(?:over|ended|end|ending|done|finished|stopped)\b",
    re.IGNORECASE,
)
PAUSE = re.compile(r"\b(?:stop|pause)\s+listening\b|\bgo to sleep\b|^\W*pause\W*$", re.IGNORECASE)
# "Hey Buddy, stop" was the natural way to turn it off in the first real test, and it
# went to Claude as an unknown command while the process kept running.
SHUTDOWN = re.compile(
    r"\bshut\s*(?:yourself\s+)?down\b|\bturn\s+(?:yourself\s+)?off\b|\bswitch\s+(?:yourself\s+)?off\b"
    r"|\bpower\s+off\b|^\W*stop(?:\W+(?:buddy|now|please))*\W*$",
    re.IGNORECASE,
)
CANCEL = re.compile(r"^\W*(?:cancel(?:\W+that)?|never\s*mind|forget\s+(?:it|that)|scratch\s+that)\W*$", re.IGNORECASE)
LOCAL_COMMANDS = {"start_meeting", "stop_meeting", "pause", "shutdown"}

SUMMARY_PENDING = (
    "# Summary\n\n_Pending: the Claude Code session following Buddy writes this when the "
    "meeting ends._\n"
)


def parse_wake(text: str) -> tuple[bool, str]:
    """Whether an utterance opens with the wake phrase, and what followed it."""
    match = WAKE.match(text or "")
    if not match:
        return False, ""
    return True, match.group(1).strip()


def classify_command(text: str) -> str:
    """Commands Buddy settles itself, instantly, without waiting on Claude.

    Anything ambiguous goes to Claude, which can still run `buddy meeting start|stop`:
    starting or ending a recording by mistake is worse than taking a minute longer.
    """
    if PAUSE.search(text):
        return "pause"
    starts = bool(START_MEETING.search(text))
    if SHUTDOWN.search(text) and not STOP_MEETING.search(text):
        return "shutdown"
    stops = bool(STOP_MEETING.search(text))
    if starts and not stops:
        return "start_meeting"
    if stops and not starts:
        return "stop_meeting"
    return "other"


def is_cancel(text: str) -> bool:
    return bool(CANCEL.match(text or ""))


def end_phrase_pattern(phrases: list[str]) -> re.Pattern[str]:
    """Match a closing phrase at the end of an utterance, whatever Whisper did to punctuation."""
    def word(text: str) -> str:
        # Whisper writes "that's" with a straight or a curly apostrophe.
        return "['\u2019]".join(re.escape(part) for part in re.split("['\u2019]", text))

    alternatives = [r"\W+".join(word(w) for w in phrase.split()) for phrase in phrases if phrase.strip()]
    return re.compile(rf"(?:^|\W)(?:{'|'.join(alternatives) or '(?!)'})\W*$", re.IGNORECASE)


def split_end_phrase(text: str, pattern: re.Pattern[str]) -> tuple[str, bool]:
    """The command text before a closing phrase, and whether the phrase was there."""
    match = pattern.search(text or "")
    if not match:
        return (text or "").strip(), False
    return text[: match.start()].strip(" ,.;:-"), True


def _process_started(pid: int) -> str:
    return datetime.fromtimestamp(psutil.Process(pid).create_time(), timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def process_alive(state: dict[str, Any]) -> bool:
    """A recorded pid is only ours if the process also started when the record says."""
    try:
        process = psutil.Process(int(state["pid"]))
        started = datetime.fromisoformat(str(state["started"]).replace("Z", "+00:00"))
        return process.is_running() and abs(process.create_time() - started.timestamp()) < 60
    except (psutil.Error, OSError, ValueError, TypeError, KeyError):
        return False


def listener_state(runtime_dir: Path = DEFAULT_RUNTIME_DIR) -> dict[str, Any] | None:
    state = _read_json(runtime_dir / "listen.json")
    return state if state and process_alive(state) else None


# ------------------------------------------------------------------ event log


class EventLog:
    """Append-only record of what the listener noticed, shared by every watcher.

    Like the Slack watcher's state, it lives on disk rather than in a session, so a
    monitor that was down catches up on re-arm instead of losing what happened.
    """

    def __init__(self, path: Path, max_bytes: int = 5 * 1024 * 1024, archive: bool = True) -> None:
        self.path = path
        self.meta = path.with_name(path.stem + ".meta.json")
        self.max_bytes = max_bytes
        # A full log is set aside for the record, unless it only ever fed a screen.
        self.archive = archive

    def _next_seq(self) -> int:
        return int(_read_json(self.meta).get("seq", 0)) + 1

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        seq = self._next_seq()
        record = {"seq": seq, "time": utc_now(), "type": kind, **fields}
        if self.path.exists() and self.path.stat().st_size > self.max_bytes:
            if self.archive:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                self.path.replace(self.path.with_name(f"{self.path.stem}.{stamp}{self.path.suffix}"))
            else:
                self.path.unlink()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        _write_json(self.meta, {"seq": seq})
        return record


class EventReader:
    """One watcher's position in the event log.

    Each watcher has its own named cursor, so a second session does not swallow what
    the first one was waiting for. A new name starts at the end: replaying every past
    meeting to a freshly armed monitor would bury the one happening now.
    """

    def __init__(self, path: Path, cursor_path: Path) -> None:
        self.path = path
        self.cursor_path = cursor_path

    def load_cursor(self) -> dict[str, int]:
        saved = _read_json(self.cursor_path)
        if saved:
            return {"offset": int(saved.get("offset", 0)), "seq": int(saved.get("seq", 0))}
        return self.end_cursor()

    def end_cursor(self) -> dict[str, int]:
        """A position after everything already written: only what happens next is read."""
        size = self.path.stat().st_size if self.path.exists() else 0
        last = int(_read_json(self.path.with_name(self.path.stem + ".meta.json")).get("seq", 0))
        return {"offset": size, "seq": last}

    def save_cursor(self, cursor: dict[str, int]) -> None:
        _write_json(self.cursor_path, cursor)

    def read(self, cursor: dict[str, int]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        if not self.path.exists():
            return [], cursor
        size = self.path.stat().st_size
        offset = cursor["offset"] if cursor["offset"] <= size else 0  # the log was rotated
        with self.path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
        complete = chunk.rfind(b"\n") + 1  # a line still being written is left for later
        records: list[dict[str, Any]] = []
        seq = cursor["seq"]
        for raw in chunk[:complete].splitlines():
            try:
                record = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if int(record.get("seq", 0)) <= cursor["seq"]:
                continue
            records.append(record)
            seq = max(seq, int(record.get("seq", 0)))
        return records, {"offset": offset + complete, "seq": seq}


def format_event(record: dict[str, Any], max_chars: int = 6000) -> str:
    """One line per event: each line becomes a notification in the watching session."""
    try:
        moment = datetime.fromisoformat(str(record.get("time"))).astimezone().strftime("%H:%M:%S")
    except ValueError:
        moment = "--:--:--"
    kind = record.get("type", "EVENT")
    parts = [moment, kind]
    for key in ("meeting", "reason", "minutes"):
        if record.get(key) not in (None, ""):
            parts.append(f"{key}={record[key]}")
    head = " ".join(str(part) for part in parts)
    if kind == "MEETING_BATCH":
        lines = [str(line) for line in record.get("lines", [])]
        body = " | ".join(lines)
        text = f"{head} lines={len(lines)} | {body}"
        if len(text) > max_chars:
            text = text[: max_chars - 60].rstrip() + f" ... (full text in {record.get('transcript', 'transcript.txt')})"
        return text
    if kind == "COMMAND":
        ended = record.get("ended", "phrase")
        if ended == "typed":
            return f'{head} typed="{record.get("text", "")}"'
        # A dictation that closed on silence or length: the user may not have finished.
        note = "" if ended == "phrase" else f" ended={ended} (may be incomplete)"
        return f'{head} said="{record.get("text", "")}"{note}'
    if kind in {"MEETING_START", "MEETING_END"}:
        extra = [f"lines={record['lines']}"] if "lines" in record else []
        extra.append(f"transcript={record.get('transcript', '')}")
        return " ".join([head, *extra])
    if record.get("text"):
        return f"{head} {record['text']}"
    return head


# --------------------------------------------------------------------- batches


class MeetingBatcher:
    """Groups meeting speech into notifications a session can keep up with.

    A meeting yields a line every few seconds; a monitor that turned each into a
    notification would be stopped as a firehose, and the session would only ever be
    reading. A batch goes out at a pause, no more than once per interval, and never
    waits longer than max_wait.
    """

    def __init__(self, min_interval: float, pause: float, max_wait: float) -> None:
        self.min_interval = min_interval
        self.pause = pause
        self.max_wait = max_wait
        self.lines: list[str] = []
        self.first_at = 0.0
        self.last_at = 0.0
        self.last_emit = float("-inf")

    def add(self, line: str, now: float) -> None:
        if not self.lines:
            self.first_at = now
        self.lines.append(line)
        self.last_at = now

    def due(self, now: float) -> bool:
        if not self.lines:
            return False
        if now - self.first_at >= self.max_wait:
            return True
        return now - self.last_at >= self.pause and now - self.last_emit >= self.min_interval

    def take(self, now: float) -> list[str]:
        lines, self.lines = self.lines, []
        self.last_emit = now
        return lines


# ---------------------------------------------------------------------- daemon


class Stream(Protocol):
    def start(self) -> None: ...

    async def stop(self) -> None: ...


class CaptureStream:
    """One platform capture thread feeding one local ASR task."""

    def __init__(self, speaker: str, settings: Settings, bus: EventBus, pool: Any) -> None:
        self.speaker = speaker
        self.settings = settings
        self.bus = bus
        self.pool = pool
        self.capture: Any = None
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        import threading

        from .asr import run_local_asr
        from .audio import AudioCapture

        audio = self.settings.audio
        size = max(10, audio.queue_seconds * 1000 // audio.chunk_ms)
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=size)
        self.capture_stop = threading.Event()
        self.asr_stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        self.capture = AudioCapture(self.speaker, audio, self.queue, self.capture_stop, self.bus, loop, None)
        self.capture.start()
        self.task = asyncio.create_task(
            run_local_asr(self.queue, self.pool, self.settings, self.bus, self.asr_stop),
            name=f"listen-asr-{self.speaker.lower()}",
        )

    async def stop(self) -> None:
        self.capture_stop.set()
        await asyncio.to_thread(self.capture.join)
        self.asr_stop.set()
        if self.task is not None:
            with suppress(Exception):
                await asyncio.wait_for(self.task, timeout=15)


class ListenDaemon:
    """Buddy as an always-attentive sensor.

    The microphone is always open, but speech is only kept in two cases: it opens with
    "Hey Buddy", or a meeting is being recorded. Everything else is transcribed in
    memory and dropped. System audio is captured only during a meeting. What the
    listener notices goes to the event log, where a Claude Code session picks it up.

    What is kept is also shown as it happens: every meeting utterance and every piece
    of a dictated command goes to a display feed that Buddy's window and `buddy live`
    read. Showing needs no model, so it is not batched.
    """

    def __init__(
        self,
        settings: Settings,
        runtime_dir: Path | None = None,
        stream_factory: Callable[[str], Stream] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.runtime_dir = runtime_dir or DEFAULT_RUNTIME_DIR
        self.state_file = self.runtime_dir / "state.json"  # shared with sessions and MCP
        self.stop_file = self.runtime_dir / "stop.flag"
        self.suggest_file = self.runtime_dir / "suggest.flag"
        self.listen_file = self.runtime_dir / "listen.json"
        self.control_dir = self.runtime_dir / "listen"
        listen = settings.listen
        self.events = EventLog(self.runtime_dir / "events.jsonl", int(listen.event_log_max_mb * 1024 * 1024))
        self.live = EventLog(self.runtime_dir / "live.jsonl", 1024 * 1024, archive=False)
        self.bus = EventBus()
        self.clock = clock
        self._factory = stream_factory
        self._pool: Any = None
        self._selection: Any = None
        self.mic: Stream | None = None
        self.remote: Stream | None = None
        self.storage: Any = None
        self.paused = False
        self.batcher = self._new_batcher()
        # An open command: what the user has said since "Hey Buddy", waiting for the
        # closing phrase. None when no command is being dictated.
        self.capture: list[str] | None = None
        self._capture_started = 0.0
        self._capture_last = 0.0
        self._end_phrase = end_phrase_pattern(listen.end_phrases)
        self._shutdown_reason = ""
        self.chime: Callable[[str], None] = self._play_chime
        self._meeting_started = 0.0
        self._last_speech = 0.0
        self._started_iso = ""

    # --------------------------------------------------------------- plumbing

    def _new_batcher(self) -> MeetingBatcher:
        listen = self.settings.listen
        return MeetingBatcher(listen.batch_min_seconds, listen.batch_pause_seconds, listen.batch_max_wait_seconds)

    def _stream(self, speaker: str) -> Stream:
        if self._factory is not None:
            return self._factory(speaker)
        if self._pool is None:
            from .asr import LocalModelPool, select_asr

            self._selection = select_asr(self.settings)
            self._pool = LocalModelPool(self._selection, self.settings)
        return CaptureStream(speaker, self.settings, self.bus, self._pool)

    @property
    def meeting_id(self) -> str:
        return self.storage.directory.name if self.storage is not None else ""

    @property
    def state(self) -> str:
        if self.paused:
            return "paused"
        return "meeting" if self.storage is not None else "listening"

    def _write_listen_state(self) -> None:
        _write_json(
            self.listen_file,
            {
                "pid": os.getpid(),
                "started": self._started_iso,
                "state": self.state,
                "meeting_id": self.meeting_id,
                "meeting_dir": str(self.storage.directory) if self.storage is not None else "",
                "updated": utc_now(),
            },
        )

    def claim(self) -> None:
        """Refuse to run beside another listener or a foreground session: both want the mic."""
        if listener_state(self.runtime_dir):
            raise RuntimeError("Buddy is already listening")
        session = _read_json(self.state_file)
        if session and process_alive(session):
            raise RuntimeError("a buddy start session is active; stop it first")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        for stale in (self.state_file, self.stop_file, self.suggest_file):
            stale.unlink(missing_ok=True)
        if self.control_dir.exists():
            # Requests left by a listener that died are not replayed on the next start.
            for stale_request in [*self.control_dir.glob("*.flag"), *self.control_dir.glob("command-*.json")]:
                stale_request.unlink(missing_ok=True)
        self._started_iso = _process_started(os.getpid())
        self._write_listen_state()

    def release(self) -> None:
        self.listen_file.unlink(missing_ok=True)
        self.state_file.unlink(missing_ok=True)

    def _show(self, kind: str, **fields: Any) -> None:
        """Put something on screen. A display that fails must never stop the listening."""
        try:
            self.live.append(kind, **fields)
        except OSError:
            pass

    # --------------------------------------------------------------- listening

    async def start_listening(self) -> None:
        if self.mic is None:
            self.mic = self._stream("ME")
            self.mic.start()
        self.paused = False
        self._write_listen_state()

    async def stop_listening(self) -> None:
        if self.mic is not None:
            await self.mic.stop()
            self.mic = None

    async def pause(self, reason: str) -> None:
        if self.storage is not None:
            await self.stop_meeting(f"paused ({reason})")
        await self.stop_listening()
        self.paused = True
        self._write_listen_state()
        self.events.append("PAUSED", reason=reason, text="Microphone closed; run `buddy resume` to listen again.")

    async def resume(self, reason: str) -> None:
        if not self.paused:
            return
        await self.start_listening()
        self.events.append("RESUMED", reason=reason)

    # ---------------------------------------------------------------- meetings

    async def start_meeting(self, reason: str) -> None:
        if self.storage is not None:
            self.events.append("NOTICE", meeting=self.meeting_id, text="Already recording this meeting.")
            return
        if self.paused:
            await self.resume(reason)
        from .storage import MeetingStorage

        selection = self._selection
        info = (
            {"mode": selection.mode, "model": selection.model, "device": selection.device,
             "compute_type": selection.compute_type, "reason": selection.reason}
            if selection is not None else {"mode": "listener"}
        )
        self.storage = MeetingStorage(self.settings, info)
        self.storage.metadata["started_by"] = reason
        self.remote = self._stream("REMOTE")
        self.remote.start()
        now = self.clock()
        self._meeting_started = now
        self._last_speech = now
        self.batcher = self._new_batcher()
        _write_json(
            self.state_file,
            {
                "pid": os.getpid(),
                # Process start, not meeting start: that is what the status check compares.
                "started": self._started_iso,
                "meeting_dir": str(self.storage.directory),
                "meeting_started_at": self.storage.metadata["started_at"],
                "asr_mode": info.get("mode"),
                "model": info.get("model"),
                "recording": True,
            },
        )
        self._write_listen_state()
        self.events.append(
            "MEETING_START",
            meeting=self.meeting_id,
            reason=reason,
            transcript=str(self.storage.transcript_txt),
        )

    async def stop_meeting(self, reason: str) -> None:
        if self.storage is None:
            self.events.append("NOTICE", reason=reason, text="No meeting is being recorded.")
            return
        now = self.clock()
        self._flush_batch(now)
        if self.remote is not None:
            await self.remote.stop()
            self.remote = None
        storage, self.storage = self.storage, None
        storage.metadata["stopped_by"] = reason
        await asyncio.to_thread(storage.finish, SUMMARY_PENDING)
        self.state_file.unlink(missing_ok=True)
        self.stop_file.unlink(missing_ok=True)
        self._write_listen_state()
        self.events.append(
            "MEETING_END",
            meeting=storage.directory.name,
            reason=reason,
            minutes=round((now - self._meeting_started) / 60, 1),
            lines=len(storage.final_events),
            transcript=str(storage.transcript_txt),
            summary=str(storage.summary_md),
        )

    def _flush_batch(self, now: float) -> None:
        lines = self.batcher.take(now)
        if lines and self.storage is not None:
            self.events.append(
                "MEETING_BATCH", meeting=self.meeting_id, lines=lines, transcript=str(self.storage.transcript_txt)
            )

    # ----------------------------------------------------------------- speech

    async def on_final(self, event: TranscriptEvent) -> None:
        text = (event.text or "").strip()
        if not text:
            return
        now = self.clock()
        if event.speaker == "ME":
            matched, remainder = parse_wake(text)
            if self.capture is not None or matched:
                # Shown as heard, closing phrase included, so the user can see it landed.
                self._show("DICTATION", text=text)
                if self.storage is not None:
                    await asyncio.to_thread(self.storage.append, event)
                if self.capture is None:
                    await self.open_command(remainder, now)
                else:
                    # A second "Hey Buddy" while dictating just carries on.
                    await self.continue_command(remainder if matched else text, now)
                return
        if self.storage is None:
            return  # speech outside a meeting is heard, not kept, and not shown
        self._show("SAID", meeting=self.meeting_id, speaker=event.speaker, text=text)
        await asyncio.to_thread(self.storage.append, event)
        self.batcher.add(f"{event.speaker}: {text}", now)
        self._last_speech = now

    # --------------------------------------------------------------- commands

    async def open_command(self, remainder: str, now: float) -> None:
        """"Hey Buddy" was heard: run a short control command at once, or start dictation.

        Whisper closes a segment after a short silence, so the first real test sent
        "just to let you know, item two" to Claude while the user was still talking. A
        command now stays open until its closing phrase, however many pauses it takes.
        """
        body, _ = split_end_phrase(remainder, self._end_phrase)
        kind = classify_command(body) if body else "other"
        if kind in LOCAL_COMMANDS:
            await self.run_local(kind)
            return
        self.capture = []
        self._capture_started = now
        self._capture_last = now
        self.chime("open")
        if remainder:
            await self.continue_command(remainder, now)

    async def continue_command(self, piece: str, now: float) -> None:
        assert self.capture is not None
        if is_cancel(piece):
            self.capture = None
            self.chime("cancel")
            self._show("CANCELLED")
            return
        body, closed = split_end_phrase(piece, self._end_phrase)
        kind = classify_command(body) if body and not self.capture else "other"
        if kind in LOCAL_COMMANDS:
            # "Hey Buddy." then "the meeting is starting": still a control command.
            self.capture = None
            await self.run_local(kind)
            return
        if body:
            self.capture.append(body)
        self._capture_last = now
        if closed:
            await self.send_command("phrase")

    async def send_command(self, ended: str) -> None:
        """Close the open command and hand it on.

        `ended` records how it closed. Anything other than the closing phrase means the
        user may not have finished, and the session is told so.
        """
        words, self.capture = self.capture or [], None
        text = " ".join(words).strip()
        if not text:
            self.chime("cancel")
            self._show("CANCELLED")
            return
        kind = classify_command(text)
        if kind in LOCAL_COMMANDS:
            await self.run_local(kind)
            return
        # Only the user's own wake-worded speech ever arrives here: REMOTE speech and
        # anything said without "Hey Buddy" never becomes a command.
        self.events.append("COMMAND", meeting=self.meeting_id, text=text, ended=ended)
        self.chime("sent")

    async def run_local(self, kind: str, reason: str = "voice") -> None:
        if reason == "voice":
            self.chime("sent")
        if kind == "start_meeting":
            await self.start_meeting(reason)
        elif kind == "stop_meeting":
            await self.stop_meeting(reason)
        elif kind == "pause":
            await self.pause(reason)
        elif kind == "shutdown":
            self._shutdown_reason = reason

    async def command(self, text: str) -> None:
        """Handle a complete command, as if it had been dictated and closed."""
        self.capture = [text]
        await self.send_command("phrase")

    async def typed(self, text: str) -> None:
        """A command typed in Buddy's window or `buddy say`: complete, so it goes at once.

        It is the user at their own keyboard, so it is trusted like their microphone,
        and it works while the microphone is paused.
        """
        text = text.strip()
        if not text:
            return
        kind = classify_command(text)
        if kind in LOCAL_COMMANDS:
            await self.run_local(kind, "typed")
            return
        self.events.append("COMMAND", meeting=self.meeting_id, text=text, ended="typed")

    def _play_chime(self, kind: str) -> None:
        """A short tone, so the user knows Buddy heard them without looking at a screen."""
        if not self.settings.listen.sounds:
            return
        tones = {"open": [(880, 90)], "sent": [(660, 70), (990, 90)], "cancel": [(330, 160)]}.get(kind, [])

        def play() -> None:
            try:
                import winsound
            except ImportError:  # only Windows has a built-in tone API
                return
            for frequency, duration in tones:
                winsound.Beep(frequency, duration)

        try:
            asyncio.get_running_loop().run_in_executor(None, play)
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ ticks

    async def tick(self) -> None:
        now = self.clock()
        listen = self.settings.listen
        if self.capture is not None:
            if now - self._capture_last >= listen.command_silence_seconds:
                await self.send_command("silence")
            elif now - self._capture_started >= listen.command_max_seconds:
                await self.send_command("limit")
        if self.storage is None:
            return
        if self.batcher.due(now):
            self._flush_batch(now)
        if now - self._last_speech >= listen.meeting_idle_minutes * 60:
            await self.stop_meeting("idle")
        elif now - self._meeting_started >= listen.meeting_max_minutes * 60:
            await self.stop_meeting("max_duration")

    async def check_controls(self) -> bool:
        """Apply requests from the CLI and the MCP server. False means shut down."""
        flags = {
            "start-meeting": lambda: self.start_meeting("requested"),
            "stop-meeting": lambda: self.stop_meeting("requested"),
            "pause": lambda: self.pause("requested"),
            "resume": lambda: self.resume("requested"),
        }
        for name, action in flags.items():
            flag = self.control_dir / f"{name}.flag"
            if flag.exists():
                flag.unlink(missing_ok=True)
                await action()
        if self.stop_file.exists():  # `buddy stop` and the MCP stop_meeting tool
            self.stop_file.unlink(missing_ok=True)
            await self.stop_meeting("stop requested")
        if self.suggest_file.exists():  # the MCP request_suggestion tool
            self.suggest_file.unlink(missing_ok=True)
            self._flush_batch(self.clock())
        if self.control_dir.exists():
            for submitted in sorted(self.control_dir.glob("command-*.json")):
                text = str(_read_json(submitted).get("text", ""))
                submitted.unlink(missing_ok=True)
                await self.typed(text)
        shutdown = self.control_dir / "shutdown.flag"
        if shutdown.exists():
            shutdown.unlink(missing_ok=True)
            self._shutdown_reason = self._shutdown_reason or "shutdown requested"
        return not self._shutdown_reason

    async def run(self, stop: asyncio.Event) -> None:
        self.claim()
        queue = self.bus.subscribe(maxsize=2048)
        reason = "stopped"
        try:
            await self.start_listening()
            self.events.append("LISTENER_UP", text="Listening for \"Hey Buddy\".")
            while not stop.is_set():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                except TimeoutError:
                    event = None
                if isinstance(event, TranscriptEvent) and event.final:
                    await self.on_final(event)
                if not await self.check_controls():
                    reason = self._shutdown_reason
                    break
                await self.tick()
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if self.storage is not None:
                await self.stop_meeting("listener stopped")
            await self.stop_listening()
            self.bus.unsubscribe(queue)
            self.events.append("LISTENER_DOWN", reason=reason)
            self.release()


def request(runtime_dir: Path, name: str) -> None:
    """Ask a running listener to do something; it acts within half a second."""
    control = runtime_dir / "listen"
    control.mkdir(parents=True, exist_ok=True)
    (control / f"{name}.flag").touch()


def submit(runtime_dir: Path, text: str) -> None:
    """Hand a typed command to the running listener, which alone writes the event log."""
    control = runtime_dir / "listen"
    control.mkdir(parents=True, exist_ok=True)
    # Nanoseconds keep two quick submissions apart and in order.
    _write_json(control / f"command-{time.time_ns()}.json", {"text": text})
