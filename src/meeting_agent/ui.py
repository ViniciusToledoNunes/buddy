from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import Settings
from .events import (
    InvestigationEvent,
    Event,
    EventBus,
    StatusEvent,
    SuggestionBatchEvent,
    SuggestionEvent,
    TranscriptEvent,
)


KIND_ICON = {"COMMENT": "💡", "QUESTION": "❓", "RISK": "⚠", "CONNECTION": "🔗", "ACTION": "✅"}

# Ordered for reading; anything else a component reports is appended rather than
# dropped, so a new or failing subsystem can never go unnoticed again.
ORDERED_COMPONENTS = (
    "audio-remote",
    "audio-me",
    "asr-local",
    "asr-cloud-remote",
    "asr-cloud-me",
    "hotkeys",
    "llm",
    "copilot",
    "investigator",
    "storage",
)
HEALTHY_STATES = {"connected", "ok", "calibrated"}
BROKEN_STATES = {"failed", "degraded", "stopped"}


class LiveUI:
    def __init__(self, settings: Settings, bus: EventBus, meeting_dir: str) -> None:
        self.settings = settings
        self.bus = bus
        self.meeting_dir = meeting_dir
        self.lines: deque[tuple[str, str, str]] = deque(maxlen=14)
        self.partials: dict[tuple[str, str], str] = {}
        self.suggestions: deque[SuggestionEvent] = deque(maxlen=5)
        self.statuses: dict[str, StatusEvent] = {}
        self.investigation: InvestigationEvent | None = None
        self.latency = 0.0

    def apply_event(self, event: Event) -> None:
        if isinstance(event, TranscriptEvent):
            key = (event.speaker, event.utterance_id)
            if event.final:
                self.partials.pop(key, None)
                stamp = datetime.fromisoformat(event.timestamp).astimezone().strftime("%H:%M:%S")
                self.lines.append((stamp, event.speaker, event.text))
                if event.latency_seconds is not None:
                    self.latency = event.latency_seconds
            else:
                self.partials[key] = event.text
        elif isinstance(event, SuggestionBatchEvent):
            self.suggestions.clear()
            self.suggestions.extend(event.suggestions[:5])
        elif isinstance(event, SuggestionEvent):
            self.suggestions.appendleft(event)
        elif isinstance(event, InvestigationEvent):
            self.investigation = event
        elif isinstance(event, StatusEvent):
            self.statuses[event.component] = event

    def _render(self) -> Group:
        transcript = Table.grid(expand=True)
        transcript.add_column(width=10, no_wrap=True)
        transcript.add_column(width=9, no_wrap=True)
        transcript.add_column(ratio=1)
        for timestamp, speaker, text in self.lines:
            color = "cyan" if speaker == "ME" else "white"
            transcript.add_row(timestamp, f"[{color}]{speaker}[/{color}]", text)
        for (speaker, _), text in list(self.partials.items())[-2:]:
            transcript.add_row("…", f"[dim]{speaker}[/dim]", f"[dim]{text}[/dim]")
        if not self.lines and not self.partials:
            transcript.add_row("", "", "[dim]Waiting for speech…[/dim]")

        suggestions = Table.grid(expand=True)
        suggestions.add_column(width=16, no_wrap=True)
        suggestions.add_column(ratio=1)
        for item in self.suggestions:
            suggestions.add_row(f"{KIND_ICON.get(item.kind, '•')} {item.kind}", f'"{item.text}"')
        if not self.suggestions:
            suggestions.add_row("", "[dim]Relevant suggestions will appear here. Press Ctrl+Alt+Space now.[/dim]")

        status = Text("● RECORDING / TRANSCRIBING", style="bold red")
        status.append(f"   latency {self.latency:.2f}s" if self.latency else "")
        # "hotkeys" belongs here: on Wayland pynput cannot register a global shortcut,
        # and recording continues, so the warning is the only sign the keys are dead.
        extra = tuple(sorted(set(self.statuses) - set(ORDERED_COMPONENTS)))
        for component in ORDERED_COMPONENTS + extra:
            event = self.statuses.get(component)
            if event:
                if event.state in HEALTHY_STATES:
                    color = "green"
                elif event.state in BROKEN_STATES:
                    color = "bold red"
                else:
                    color = "yellow"
                status.append(f"   {component}: {event.state}", style=color)
        status.append(f"\nSaved incrementally: {self.meeting_dir}", style="dim")
        panels = [
            Panel(transcript, title="LIVE TRANSCRIPT", border_style="blue"),
            Panel(suggestions, title="BUDDY", border_style="magenta"),
        ]
        if self.investigation is not None:
            body = Text(self.investigation.question, style="bold")
            body.append("\n" + self.investigation.text)
            panels.append(Panel(body, title="INVESTIGATION", border_style="green"))
        panels.append(Panel(status, title="STATUS", border_style="red"))
        return Group(*panels)

    async def run(self, stop: asyncio.Event) -> None:
        queue = self.bus.subscribe(maxsize=512)
        refresh = 1 / self.settings.ui.refresh_hz
        try:
            with Live(self._render(), refresh_per_second=self.settings.ui.refresh_hz, screen=False) as live:
                while not stop.is_set():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=refresh)
                        self.apply_event(event)
                    except TimeoutError:
                        pass
                    live.update(self._render())
        finally:
            self.bus.unsubscribe(queue)
