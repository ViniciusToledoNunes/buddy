from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .copilot import REPORT_SECTIONS, LLMProvider, _extract_json

UPDATE_PROMPT = """New speech since your last update ({count} lines):
{lines}
{trigger}
Reply with the JSON object your instructions describe."""

QUESTION_TRIGGER = """
This update was sent early because of a question{to}: "{question}"
Suggest an answer to it first, whoever it was asked of, in one or two lines the user
could say out loud. Answer from what you already know; research only if it is quick."""

REPORT_PROMPT = """The meeting has ended. Write the report with exactly these Markdown sections:
{sections}

Distinguish ME's actions from other people's actions. Do not invent details.

Transcript:
{transcript}"""


class AutoBrain:
    """Buddy's own brain for a meeting that no Claude Code session is following.

    The listener sends its batches to a watching session. When none watches (its
    process ended, or none ever connected), a meeting got no suggestions and no
    summary: on 2026-10-19 a 12-minute stand-up produced 24 batches, 17 of them sent
    early for questions, and nobody read them. This runs Claude Code headless on the
    same batches instead, one call at a time, keeping one conversation per meeting.
    """

    def __init__(self, settings: Settings, meetings_dir: Path | None = None,
                 provider_factory: Callable[[], LLMProvider] | None = None) -> None:
        if provider_factory is None:
            from .claude_code import ClaudeCodeProvider

            def provider_factory() -> LLMProvider:
                return ClaudeCodeProvider(settings, meetings_dir)

        self.provider = provider_factory()
        self.numbered = 0  # suggestions are numbered across the meeting, like the session's
        self.failures = 0

    def available(self) -> bool:
        check = getattr(self.provider, "available", None)
        return bool(check()) if callable(check) else True

    def _system(self) -> str:
        build = getattr(self.provider, "system_prompt", None)
        return build() if callable(build) else ""

    async def suggest(self, lines: list[str], trigger: dict[str, Any] | None = None) -> list[str]:
        """Numbered suggestions for the speech in one batch; [] when nothing is worth saying."""
        cue = ""
        if trigger and trigger.get("trigger") == "question":
            to = " addressed to ME" if trigger.get("addressed") else ""
            cue = QUESTION_TRIGGER.format(to=to, question=trigger.get("question", ""))
        prompt = UPDATE_PROMPT.format(count=len(lines), lines="\n".join(lines), trigger=cue)
        raw = await self.provider.complete(self._system(), prompt)
        data = _extract_json(raw)
        out = []
        for item in data.get("suggestions", [])[:3]:
            text = str(item.get("text", "") if isinstance(item, dict) else item).strip()
            if text:
                self.numbered += 1
                out.append(f"{self.numbered}. {text}")
        return out

    async def report(self, transcript: str) -> str:
        if not transcript.strip():
            return REPORT_SECTIONS + "\n\nNo speech was transcribed."
        from .claude_code import REPORT_SYSTEM_PROMPT

        prompt = REPORT_PROMPT.format(sections=REPORT_SECTIONS, transcript=transcript)
        return await self.provider.complete(REPORT_SYSTEM_PROMPT, prompt)


class BrainRunner:
    """Feeds batches to an AutoBrain one call at a time, without blocking the listener.

    A batch that arrives while a call is running waits and goes out with the next one,
    so a slow answer never piles up calls; a question that waits keeps its trigger.
    """

    def __init__(self, brain: AutoBrain, show: Callable[[list[str]], None],
                 notice: Callable[[str], None]) -> None:
        self.brain = brain
        self.show = show
        self.notice = notice
        self._pending: list[str] = []
        self._trigger: dict[str, Any] | None = None
        self._task: asyncio.Task[None] | None = None

    def submit(self, lines: list[str], trigger: dict[str, Any] | None = None) -> None:
        self._pending.extend(lines)
        if trigger and trigger.get("trigger") == "question":
            self._trigger = trigger  # the latest question is the one worth answering
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while self._pending:
            lines, self._pending = self._pending, []
            trigger, self._trigger = self._trigger, None
            try:
                suggestions = await self.brain.suggest(lines, trigger)
            except Exception as exc:  # a failed call must never stop the listening
                self.brain.failures += 1
                self.notice(f"Built-in brain failed: {type(exc).__name__}: {exc}")
                continue
            if suggestions:
                self.show(suggestions)

    async def finish(self, timeout: float) -> None:
        """Let the call in flight end, so the last suggestions are not lost."""
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout)
            except (TimeoutError, asyncio.TimeoutError):
                pass
