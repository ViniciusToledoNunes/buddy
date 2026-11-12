from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Callable

from .config import Settings, project_root
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
    early for questions, and nobody read them. This runs the `brain` harness headless
    on the same batches instead, one call at a time, keeping one conversation per meeting.
    """

    def __init__(self, settings: Settings, meetings_dir: Path | None = None,
                 provider_factory: Callable[[], LLMProvider] | None = None) -> None:
        if provider_factory is None:
            from .harness import harness_for

            def provider_factory() -> LLMProvider:
                return harness_for(settings).provider(settings, meetings_dir)

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


COMMAND_INTRO = """You are Buddy's brain. The user talks to Buddy from its small floating window, typing
or dictating, and no editor session is following Buddy right now, so you answer them. You
already know the user and their work from your loaded context; use it.

Reply in plain text or short Markdown, in the language the user wrote in: it is read in a small
window. Say what you did or what you found, without preamble.

A message with `ACTION_APPROVED id=N` means the user clicked Approve on card N, which was
proposed to them. That click approves exactly the content shown: do it now, as written in `detail`,
without asking again. First `BUDDY_COMMAND action N running`, then
`BUDDY_COMMAND action N done --result "<one line>" [--link <where>]`, or
`failed --result "<why>"` when it could not be done."""

QUIET_REPLY = "NOTHING"

# The skill teaches a watching session how to act on commands; the built-in brain follows
# the same rules, so the two never disagree on what needs the user's approval.
COMMAND_SKILL_SECTIONS = ("Action cards", "Work board", "Commands", "Boundaries")


def _skill_section(text: str, title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return f"## {title}\n{match.group(1).strip()}" if match else ""


def command_system_prompt(buddy_command: str, skill: Path | None = None, memory: str = "") -> str:
    path = skill or project_root() / "skills" / "buddy-listener" / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    sections = [_skill_section(text, title) for title in COMMAND_SKILL_SECTIONS]
    prompt = "\n\n".join([COMMAND_INTRO, *filter(None, sections)]).replace("BUDDY_COMMAND", buddy_command)
    return f"{prompt}\n\n{memory}" if memory else prompt


class CommandRunner:
    """Answers what the user types or dictates in the window when no session follows Buddy.

    One call at a time, in one conversation that lasts while the listener runs, so a
    follow-up can say "that one". A switch of harness or model in the window starts a
    new conversation with the new one.
    """

    def __init__(
        self,
        settings: Callable[[], Settings],
        meetings_dir: Path | None,
        reply: Callable[[str, str], None],
        notice: Callable[[str], None],
        working: Callable[[str], None] = lambda label: None,
        provider_factory: Callable[[Settings], LLMProvider] | None = None,
    ) -> None:
        self.settings = settings
        self.meetings_dir = meetings_dir
        self.reply = reply
        self.notice = notice
        self.working = working
        self.provider_factory = provider_factory or self._harness_provider
        self.provider: LLMProvider | None = None
        self._choice: tuple[str, str] | None = None
        self._pending: list[tuple[str, bool]] = []
        self._task: asyncio.Task[None] | None = None

    def _harness_provider(self, settings: Settings) -> LLMProvider:
        from .harness import harness_for

        return harness_for(settings).provider(settings, self.meetings_dir)

    def system_prompt(self, settings: Settings) -> str:
        from .claude_code import buddy_tool_command
        from .working_memory import memory_prompt

        writer = f"buddy-{settings.brain.harness}"
        return command_system_prompt(buddy_tool_command(), memory=memory_prompt(settings.memory.directory(), writer))

    def submit(self, prompt: str, quiet: bool = False) -> None:
        """Queue a prompt. A quiet one is work Buddy started itself: no "on it" line, and a
        reply of exactly QUIET_REPLY means there was nothing to tell the user."""
        self._pending.append((prompt, quiet))
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    def _provider_for(self, settings: Settings) -> LLMProvider:
        choice = (settings.brain.harness, settings.brain.model)
        if self.provider is None or choice != self._choice:
            self.provider = self.provider_factory(settings)
            self._choice = choice
        return self.provider

    async def _drain(self) -> None:
        from .harness import harness_for

        while self._pending:
            prompt, quiet = self._pending.pop(0)
            settings = self.settings()
            label = harness_for(settings).label
            provider = self._provider_for(settings)
            if not provider.available():
                self.notice(f"Nobody can answer: no session follows Buddy and {label} is not available here.")
                continue
            if not quiet:
                self.working(label)
            try:
                answer = (await provider.complete(self.system_prompt(settings), prompt)).strip()
            except Exception as exc:  # a failed call must never stop the listening
                self.notice(f"{label} could not answer: {type(exc).__name__}: {exc}")
                continue
            if quiet and answer.strip("`*. ").upper() == QUIET_REPLY:
                continue
            self.reply(label, answer or "(no reply)")

    async def finish(self, timeout: float) -> None:
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout)
            except (TimeoutError, asyncio.TimeoutError):
                pass
