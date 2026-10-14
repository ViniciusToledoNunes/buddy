from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Generic, TypeVar

T = TypeVar("T")

_WORD = re.compile(r"[\w']+")


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def coverage(said: list[str], heard: list[str]) -> float:
    """Share of `said`, in order, that also appears in `heard`."""
    if not said or not heard:
        return 0.0
    matcher = SequenceMatcher(None, said, heard, autojunk=False)
    return sum(block.size for block in matcher.get_matching_blocks()) / len(said)


@dataclass(slots=True)
class _Held(Generic[T]):
    item: T
    words: list[str]
    at: float


class EchoGuard(Generic[T]):
    """Drops microphone lines that are only the meeting's own audio coming out of the speakers.

    Without headphones the microphone hears the other side, so the same sentence is
    transcribed twice: once from system audio (REMOTE) and once, a little garbled, from
    the room (ME). On 2026-10-09 that was 77-243 lines per meeting. The two copies reach
    the recognizer at different moments and either one can finish first (the ME copy
    came first in two thirds of the pairs, sometimes 8 s apart), so a ME line is held for
    `window_seconds` and kept only if no matching REMOTE line shows up around it.

    Replaying those meetings, an 8 s window with a 0.6 threshold dropped 68% of the ME
    lines, and wrongly matched 3.4% when REMOTE was shifted two minutes (a stand-in for
    real speech). The rest are echoes REMOTE transcribed too differently to match;
    headphones remain the only full fix.
    """

    def __init__(self, window_seconds: float = 8.0, threshold: float = 0.6, min_words: int = 3) -> None:
        self.window = window_seconds
        self.threshold = threshold
        self.min_words = min_words
        self._remote: deque[tuple[float, list[str]]] = deque()
        self._held: deque[_Held[T]] = deque()
        self.dropped = 0

    @property
    def enabled(self) -> bool:
        return self.window > 0

    def _is_echo(self, said: list[str], at: float) -> bool:
        heard = [w for t, ws in self._remote if abs(t - at) <= self.window for w in ws]
        if not heard:
            return False
        # A short line ("yeah, sounds good") is a real reply far more often than an echo,
        # so it only counts as one when every word of it was just played.
        needed = 1.0 if len(said) < self.min_words else self.threshold
        return coverage(said, heard) >= needed

    def remote(self, text: str, now: float) -> list[T]:
        """Record a REMOTE line; return the held ME lines it shows to be echoes."""
        if not self.enabled:
            return []
        self._remote.append((now, words(text)))
        while self._remote and now - self._remote[0][0] > 2 * self.window:
            self._remote.popleft()
        echoes = [held for held in self._held if self._is_echo(held.words, held.at)]
        for held in echoes:
            self._held.remove(held)
        self.dropped += len(echoes)
        return [held.item for held in echoes]

    def mine(self, item: T, text: str, now: float) -> bool:
        """Hold a ME line. False means it is already known to be an echo and was dropped."""
        said = words(text)
        if self.enabled and said and self._is_echo(said, now):
            self.dropped += 1
            return False
        self._held.append(_Held(item, said, now))
        return True

    def release(self, now: float, everything: bool = False) -> list[tuple[T, float]]:
        """ME lines whose window has passed with no matching REMOTE line, with when they were said."""
        out: list[tuple[T, float]] = []
        while self._held and (everything or now - self._held[0].at >= self.window):
            held = self._held.popleft()
            out.append((held.item, held.at))
        return out
