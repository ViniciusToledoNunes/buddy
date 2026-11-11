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
    # Who on the other side said it (listen.speaker_names): the person, this meeting's
    # label for the voice ("B"), and whether the name is settled.
    name: str | None = None
    voice: str | None = None
    name_sure: bool = False
    # In memory only, never written: the voice embedding of a REMOTE line and when it was
    # spoken (monotonic clock), which is what the screen captures are matched against.
    embedding: Any = field(default=None, repr=False, compare=False)
    spoken_from: float | None = field(default=None, repr=False, compare=False)
    spoken_to: float | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for transient in ("embedding", "spoken_from", "spoken_to"):
            data.pop(transient, None)
        if self.name is None and self.voice is None:
            for unset in ("name", "voice", "name_sure"):
                data.pop(unset, None)
        return data

    @property
    def who(self) -> str:
        """The speaker as written in the transcript: "REMOTE (Nick Webb)", "REMOTE (voice B)"."""
        if self.name:
            return f"{self.speaker} ({self.name}{'' if self.name_sure else '?'})"
        if self.voice:
            return f"{self.speaker} (voice {self.voice})"
        return self.speaker


@dataclass(slots=True)
class SpeechEvent:
    """Speech started, or is still going, on one side: the moment to look at the screen."""

    speaker: Literal["ME", "REMOTE"]
    at: float  # monotonic clock


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


Event = TranscriptEvent | SpeechEvent | SuggestionEvent | SuggestionBatchEvent | InvestigationEvent | StatusEvent


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
