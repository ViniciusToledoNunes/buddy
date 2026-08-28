from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class TranscriptEvent:
    speaker: Literal["ME", "REMOTE"]
    text: str
    final: bool
    utterance_id: str
    timestamp: str = field(default_factory=utc_now)
    start_seconds: float | None = None
    end_seconds: float | None = None
    latency_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SuggestionEvent:
    kind: str
    text: str
    reason: str = ""
    timestamp: str = field(default_factory=utc_now)


@dataclass(slots=True)
class SuggestionBatchEvent:
    revision: int
    suggestions: list[SuggestionEvent]
    timestamp: str = field(default_factory=utc_now)


@dataclass(slots=True)
class InvestigationEvent:
    """An answer the investigator went and checked.

    It costs a tool loop to produce, so it holds its own place on screen instead of
    competing with a panel that is replaced every few seconds.
    """

    question: str
    text: str
    timestamp: str = field(default_factory=utc_now)


@dataclass(slots=True)
class StatusEvent:
    component: str
    state: str
    detail: str = ""
    timestamp: str = field(default_factory=utc_now)


Event = TranscriptEvent | SuggestionEvent | SuggestionBatchEvent | InvestigationEvent | StatusEvent


class EventBus:
    """Non-blocking fan-out bus. A slow consumer loses its oldest event."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def subscribe(self, maxsize: int = 256) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: Event) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
