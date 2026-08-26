from __future__ import annotations

import asyncio
import queue
import threading

from .events import EventBus, SuggestionEvent


class SuggestionOverlay:
    def __init__(self) -> None:
        self.messages: queue.Queue[SuggestionEvent | None] = queue.Queue(maxsize=20)
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run_tk, name="suggestion-overlay", daemon=True)
        self.thread.start()

    def push(self, event: SuggestionEvent) -> None:
        try:
            self.messages.put_nowait(event)
        except queue.Full:
            try:
                self.messages.get_nowait()
            except queue.Empty:
                pass
            self.messages.put_nowait(event)

    def stop(self) -> None:
        try:
            self.messages.put_nowait(None)
        except queue.Full:
            pass

    def _run_tk(self) -> None:
        import tkinter as tk

        root = tk.Tk()
        root.title("Buddy - Meeting Copilot")
        root.attributes("-topmost", True)
        root.geometry("430x150+20+20")
        root.configure(bg="#111827")
        label = tk.Label(
            root,
            text="Buddy is ready\nCtrl+Alt+Space: suggest now",
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
                    event = self.messages.get_nowait()
                    if event is None:
                        root.destroy()
                        return
                    label.configure(text=f"{event.kind}\n{event.text}")
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
            if isinstance(event, SuggestionEvent):
                overlay.push(event)
    finally:
        bus.unsubscribe(events)
        overlay.stop()
