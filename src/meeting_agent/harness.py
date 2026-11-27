"""The agent that is Buddy's brain when Buddy runs one headless.

`llm_provider: harness` and the listener's auto brain both run the harness named in
`brain.harness`. Each harness builds the provider that drives its CLI and says whether it
can run on this machine, without spending a run to find out.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from .config import Settings

if TYPE_CHECKING:
    from .copilot import LLMProvider


@dataclass(frozen=True)
class Readiness:
    state: Literal["ok", "warning", "failed"]
    detail: str


class Harness(Protocol):
    name: str
    label: str

    def provider(self, settings: Settings, meetings_dir: Path | None = None) -> LLMProvider: ...

    def readiness(self, settings: Settings) -> Readiness: ...

    def models(self) -> list[tuple[str, str]]:
        """(id, name) of the models brain.model may name; may run the CLI for a few seconds."""
        ...


class ClaudeCodeHarness:
    name = "claude-code"
    label = "Claude Code"
    context_files = ("CLAUDE.md", "CLAUDE.local.md")

    def models(self) -> list[tuple[str, str]]:
        # The CLI cannot list them; these aliases follow the newest model of each family.
        return [("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku")]

    def provider(self, settings: Settings, meetings_dir: Path | None = None) -> LLMProvider:
        from .claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider(settings, meetings_dir)

    def readiness(self, settings: Settings) -> Readiness:
        from . import claude_code

        workdir = settings.brain.directory()
        if claude_code.claude_binary() is None:
            return Readiness("failed", "the claude-code harness is selected but the claude CLI is not installed")
        if not workdir.is_dir():
            return Readiness("failed", f"brain.workdir does not exist: {workdir}")
        found = [name for name in self.context_files if (workdir / name).is_file()]
        detail = f"Claude Code in {workdir}; context files: {', '.join(found) or 'none found'}"
        return Readiness("ok" if found else "warning", detail)


class CursorHarness:
    name = "cursor"
    label = "Cursor"

    def provider(self, settings: Settings, meetings_dir: Path | None = None) -> LLMProvider:
        from .cursor_agent import CursorAgentProvider

        return CursorAgentProvider(settings, meetings_dir)

    def models(self) -> list[tuple[str, str]]:
        from .cursor_agent import list_models

        return list_models()

    def readiness(self, settings: Settings) -> Readiness:
        from . import cursor_agent

        workdir = settings.brain.directory()
        if cursor_agent.cursor_agent_binary() is None:
            return Readiness("failed", "the cursor harness is selected but the Cursor CLI (cursor-agent) is not installed")
        if not workdir.is_dir():
            return Readiness("failed", f"brain.workdir does not exist: {workdir}")
        model = settings.brain.model or "Cursor's default model"
        return Readiness("ok", f"Cursor agent in {workdir} with {model}; it loads your Cursor rules, hooks and MCP servers")


HARNESSES: dict[str, Harness] = {harness.name: harness for harness in (ClaudeCodeHarness(), CursorHarness())}


def harness_for(settings: Settings) -> Harness:
    return HARNESSES[settings.brain.harness]
