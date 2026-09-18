"""What Buddy's window and `buddy live` show, as it happens.

Three sources, none of them a model:

- the display feed (`live.jsonl`): meeting speech and dictated commands, as heard;
- the event log (`events.jsonl`): commands sent, meetings starting and ending, pauses;
- the Claude Code session that watches Buddy: its own record of the conversation,
  which Claude Code keeps on disk, read to show what it is doing and what it said.

Reading that record costs nothing and changes nothing, so the session stays the brain
with everything it already knows. Its format belongs to Claude Code and may change:
anything unreadable is skipped, and the rest of the view keeps working.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .events import utc_now
from .listener import EventReader, _process_started, _read_json, _write_json, listener_state, process_alive

# The description the buddy-listener skill gives its Monitor.
BUDDY_MONITOR = 'Monitor event: "Buddy listener'
# "3. Ask who owns the rollback plan", tolerating the bold markers Claude likes.
NUMBERED = re.compile(r"^\s*(?:\*\*)?(\d{1,3})[.)](?:\*\*)?\s+(.+?)\s*$")
SESSION_ID = re.compile(r"[0-9A-Za-z-]{8,64}")


@dataclass(slots=True)
class LiveItem:
    """One line on screen."""

    kind: str  # said, dictation, command, cancelled, state, notice, claude, tool, you
    text: str
    who: str = ""
    note: str = ""
    at: float = 0.0  # epoch seconds, to merge sources in order

    @property
    def clock(self) -> str:
        return datetime.fromtimestamp(self.at).strftime("%H:%M:%S") if self.at else ""

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "clock": self.clock}


def _epoch(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


# ------------------------------------------------------------ listener sources


def item_from_feed(record: dict[str, Any]) -> LiveItem | None:
    at = _epoch(record.get("time"))
    kind = record.get("type")
    if kind == "SAID":
        return LiveItem("said", str(record.get("text", "")), who=str(record.get("speaker", "")), at=at)
    if kind == "DICTATION":
        return LiveItem("dictation", str(record.get("text", "")), who="ME", at=at)
    if kind == "CANCELLED":
        return LiveItem("cancelled", "Command dropped.", at=at)
    return None


STATES = {
    "LISTENER_UP": 'Listening for "Hey Buddy".',
    "LISTENER_DOWN": "Buddy is off.",
    "PAUSED": "Paused: the microphone is closed.",
    "RESUMED": "Listening again.",
    "MEETING_START": "Meeting started: recording.",
}


def item_from_event(record: dict[str, Any]) -> LiveItem | None:
    at = _epoch(record.get("time"))
    kind = record.get("type")
    if kind == "COMMAND":
        ended = record.get("ended", "phrase")
        note = "sent after a silence; may be incomplete" if ended in {"silence", "limit"} else ""
        return LiveItem("command", str(record.get("text", "")), who="typed" if ended == "typed" else "voice",
                        note=note, at=at)
    if kind == "MEETING_END":
        return LiveItem("state", f"Meeting ended after {record.get('minutes', '?')} min.", at=at)
    if kind in STATES:
        return LiveItem("state", STATES[kind], at=at)
    if kind == "NOTICE":
        return LiveItem("notice", str(record.get("text", "")), at=at)
    return None  # MEETING_BATCH: its lines were already shown as they were said


# ----------------------------------------------------------- the brain session


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def find_session_file(session_id: str, home: Path | None = None) -> Path | None:
    """Where Claude Code keeps a session's conversation: projects/<folder>/<id>.jsonl."""
    if not SESSION_ID.fullmatch(session_id or ""):
        return None  # it goes into a glob
    candidates = list(((home or claude_home()) / "projects").glob(f"*/{session_id}.jsonl"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def register_brain(runtime_dir: Path, session_id: str, consumer: str) -> None:
    """Record which session watches Buddy. The watch process calls this from inside it."""
    _write_json(
        runtime_dir / "brain.json",
        {"session_id": session_id, "consumer": consumer, "pid": os.getpid(),
         "started": _process_started(os.getpid()), "registered": utc_now()},
    )


def unregister_brain(runtime_dir: Path) -> None:
    """Drop the record, unless a newer watch has replaced it since."""
    path = runtime_dir / "brain.json"
    if _read_json(path).get("pid") == os.getpid():
        path.unlink(missing_ok=True)


def brain_state(runtime_dir: Path) -> dict[str, Any] | None:
    state = _read_json(runtime_dir / "brain.json")
    if not state.get("session_id"):
        return None
    return {**state, "watching": process_alive(state)}


def numbered_items(text: str) -> list[str]:
    items = []
    for line in text.splitlines():
        match = NUMBERED.match(line)
        if match:
            items.append(f"{match.group(1)}. {match.group(2).replace('**', '')}")
    return items


def describe_tool(name: str, data: Any) -> str:
    """A tool call in a few words: what Claude is doing, not how."""
    data = data if isinstance(data, dict) else {}

    def pick(*keys: str) -> str:
        for key in keys:
            if data.get(key):
                return str(data[key]).strip().splitlines()[0][:140]
        return ""

    if name in {"Bash", "PowerShell"}:
        return pick("description") or f"$ {pick('command')}"
    if name in {"Read", "Write", "Edit", "NotebookEdit"}:
        return f"{name} {Path(pick('file_path', 'notebook_path')).name}"
    if name in {"Grep", "Glob"}:
        return f"{name} {pick('pattern')}"
    if name == "WebFetch":
        return f"Fetch {pick('url')}"
    if name == "WebSearch":
        return f"Search {pick('query')}"
    if name == "Skill":
        return f"Skill {pick('skill')}"
    if name in {"Agent", "Task", "Monitor"}:
        return f"{name}: {pick('description')}"
    if name.startswith("mcp__"):
        return ": ".join(name.split("__")[1:])
    return name


class SessionMirror:
    """Follows a Claude Code session's record and turns it into lines on screen.

    A turn is started by a Buddy event, by the user in the chat, or by something else
    (another monitor, a background task). By default only Buddy's turns are shown; the
    rest of the session is the user's own business.
    """

    def __init__(self, path: Path, show: str = "buddy") -> None:
        self.path = path
        self.show = show
        self.offset = path.stat().st_size if path.exists() else 0  # from now on
        self.origin = ""
        self.batch_turn = False
        self.suggestions: list[str] = []
        self.suggestions_changed = False

    def _visible(self) -> bool:
        return self.show == "all" or self.origin == "buddy"

    def _start_turn(self, prompt: str) -> None:
        if "<task-notification>" in prompt:
            self.origin = "buddy" if BUDDY_MONITOR in prompt else "other"
        else:
            self.origin = "user"
        # A reply to meeting speech is where the suggestions are.
        self.batch_turn = self.origin == "buddy" and " MEETING_BATCH " in prompt

    def poll(self) -> list[LiveItem]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            self.offset = 0  # replaced
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
        except OSError:
            return []
        complete = chunk.rfind(b"\n") + 1  # a line still being written waits
        self.offset += complete
        items: list[LiveItem] = []
        for raw in chunk[:complete].splitlines():
            try:
                record = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(record, dict):
                items.extend(self._handle(record))
        return items

    def _handle(self, record: dict[str, Any]) -> list[LiveItem]:
        if record.get("isSidechain"):
            return []  # a subagent's own conversation
        at = _epoch(record.get("timestamp"))
        kind = record.get("type")
        if kind == "attachment":
            attachment = record.get("attachment")
            if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
                prompt = str(attachment.get("prompt", ""))
                self._start_turn(prompt)
                if self.origin == "user" and self.show == "all":
                    return [LiveItem("you", prompt, at=at)]
            return []
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content")
        if kind == "user":
            if record.get("isMeta"):
                return []
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in blocks):
                return []
            text = "\n".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text")
            text = text.strip()
            if not text or (text.startswith("<") and "<task-notification>" not in text):
                return []  # slash-command plumbing and system notes
            self._start_turn(text)
            if self.origin == "user" and self.show == "all":
                return [LiveItem("you", text, at=at)]
            return []
        if kind != "assistant" or not isinstance(content, list):
            return []
        items: list[LiveItem] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text", "")).strip():
                text = str(block["text"]).strip()
                if self.batch_turn:
                    found = numbered_items(text)
                    if found:
                        self.suggestions = found
                        self.suggestions_changed = True
                if self._visible():
                    items.append(LiveItem("claude", text, at=at))
            elif block.get("type") == "tool_use" and self._visible():
                items.append(LiveItem("tool", describe_tool(str(block.get("name", "")), block.get("input")), at=at))
        return items


# ------------------------------------------------------------------ the view


class LiveView:
    """Everything on screen, merged in time order, from the moment the view opens."""

    def __init__(self, runtime_dir: Path, show: str = "buddy", home: Path | None = None) -> None:
        self.runtime_dir = runtime_dir
        self.show = show
        self.home = home
        self.events = EventReader(runtime_dir / "events.jsonl", runtime_dir / "cursors" / "unused")
        self.event_cursor = self.events.end_cursor()
        self.feed = EventReader(runtime_dir / "live.jsonl", runtime_dir / "cursors" / "unused")
        self.feed_cursor = self.feed.end_cursor()
        self.mirror: SessionMirror | None = None
        self.session_id = ""
        self._next_lookup = 0.0
        self.suggestions: list[str] = []

    def set_show(self, show: str) -> None:
        self.show = show
        if self.mirror is not None:
            self.mirror.show = show

    def _follow_brain(self) -> None:
        brain = brain_state(self.runtime_dir)
        if not brain or brain["session_id"] == self.session_id or time.monotonic() < self._next_lookup:
            return
        path = find_session_file(str(brain["session_id"]), self.home)
        if path is None:
            self._next_lookup = time.monotonic() + 5  # not written yet; look again shortly
            return
        self.session_id = str(brain["session_id"])
        self.mirror = SessionMirror(path, self.show)

    def poll(self) -> list[LiveItem]:
        items: list[LiveItem] = []
        records, self.event_cursor = self.events.read(self.event_cursor)
        for record in records:
            if record.get("type") == "MEETING_START":
                self.suggestions = []  # the last meeting's advice does not carry over
            item = item_from_event(record)
            if item is not None:
                items.append(item)
        records, self.feed_cursor = self.feed.read(self.feed_cursor)
        items.extend(item for item in map(item_from_feed, records) if item is not None)
        self._follow_brain()
        if self.mirror is not None:
            items.extend(self.mirror.poll())
            if self.mirror.suggestions_changed:
                self.suggestions = list(self.mirror.suggestions)
                self.mirror.suggestions_changed = False
        return sorted(items, key=lambda item: item.at)

    def status(self) -> dict[str, Any]:
        listener = listener_state(self.runtime_dir)
        brain = brain_state(self.runtime_dir)
        return {
            "listener": listener.get("state", "listening") if listener else "off",
            "meeting": listener.get("meeting_id", "") if listener else "",
            # watching: a session follows Buddy. idle: it did, but its monitor is down, so
            # commands wait on disk until it re-arms. none: no session has ever connected.
            "brain": ("watching" if brain["watching"] else "idle") if brain else "none",
            "suggestions": list(self.suggestions),
            "show": self.show,
        }


# ------------------------------------------------------------------ terminal

STYLES = {
    "said": "white", "dictation": "cyan", "command": "bold cyan", "cancelled": "yellow",
    "state": "magenta", "notice": "yellow", "claude": "green", "tool": "dim", "you": "blue",
}
LABELS = {
    "dictation": "ME ▸", "cancelled": "", "state": "", "notice": "", "claude": "Claude", "tool": "  ⚙", "you": "you",
}


def terminal_line(item: LiveItem) -> str:
    """Rich markup for one item; the text itself is escaped, never interpreted."""
    from rich.markup import escape

    if item.kind == "said":
        label = item.who
    elif item.kind == "command":
        label = f"{item.who} ➜"
    else:
        label = LABELS.get(item.kind, item.kind)
    head = f"{label} " if label else ""
    note = f" [dim]({escape(item.note)})[/dim]" if item.note else ""
    style = STYLES.get(item.kind, "white")
    return f"[dim]{item.clock}[/dim] [{style}]{escape(head + item.text)}[/{style}]{note}"
