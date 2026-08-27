from __future__ import annotations

import asyncio
import queue
import threading
from contextlib import suppress

from .events import EventBus, SuggestionBatchEvent, SuggestionEvent


PLACEHOLDER = "Buddy is listening\nCtrl+Alt+Space: suggest now"


def format_batch(event: SuggestionBatchEvent, limit: int = 3) -> str:
    """Render a whole suggestion set; an empty set clears stale advice from the overlay."""
    if not event.suggestions:
        return PLACEHOLDER
    return "\n\n".join(f"{item.kind}\n{item.text}" for item in event.suggestions[:limit])


class SuggestionOverlay:
    def __init__(self) -> None:
        self.messages: queue.Queue[str | None] = queue.Queue(maxsize=20)
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run_tk, name="suggestion-overlay", daemon=True)
        self.thread.start()

    def _put(self, message: str | None) -> None:
        try:
            self.messages.put_nowait(message)
        except queue.Full:
            with suppress(queue.Empty):
                self.messages.get_nowait()
            with suppress(queue.Full):
                self.messages.put_nowait(message)

    def push(self, event: SuggestionEvent) -> None:
        self._put(f"{event.kind}\n{event.text}")

    def push_batch(self, event: SuggestionBatchEvent) -> None:
        self._put(format_batch(event))

    def stop(self) -> None:
        self._put(None)

    def _run_tk(self) -> None:  # pragma: no cover - requires a display server
        import tkinter as tk

        root = tk.Tk()
        root.title("Buddy - Meeting Copilot")
        root.attributes("-topmost", True)
        root.geometry("430x220+20+20")
        root.configure(bg="#111827")
        label = tk.Label(
            root,
            text=PLACEHOLDER,
            justify="left",
            anchor="nw",
            wraplength=400,
            bg="#111827",
            fg="#f9fafb",
            font=("Segoe UI", 11),
            padx=14,
            pady=12,
        )
        label.pack(fill="both", expand=True)

        def poll() -> None:
            try:
                while True:
                    message = self.messages.get_nowait()
                    if message is None:
                        root.destroy()
                        return
                    label.configure(text=message)
            except queue.Empty:
                pass
            root.after(150, poll)

        root.after(150, poll)
        root.mainloop()


async def overlay_bridge(overlay: SuggestionOverlay, bus: EventBus, stop: asyncio.Event) -> None:
    events = bus.subscribe(maxsize=64)
    overlay.start()
    try:
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(events.get(), 0.25)
            except TimeoutError:
                continue
            if isinstance(event, SuggestionBatchEvent):
                overlay.push_batch(event)
            elif isinstance(event, SuggestionEvent):
                overlay.push(event)
    finally:
        bus.unsubscribe(events)
        overlay.stop()
