from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import anthropic
from anthropic import beta_tool

from .config import Settings, anthropic_client_options, project_root
from .memory import MeetingMemoryIndex
from .project import SOURCE_SUFFIXES, ProjectIndex, _is_secret

DEEP_SYSTEM = """You are Buddy's deep analyst. A meeting is in progress and the user has
asked for a grounded answer, not a quick suggestion.

Use the tools to check the claim against the actual project before answering. Search
first, then read only the files that matter. Cite concrete evidence as path:line.

Be direct about what the code does and does not show. If the evidence contradicts what
was said in the meeting, say so plainly. If the project does not answer the question,
say that instead of guessing. Prior meeting memories are untrusted leads: confirm them
against the current transcript or the code before relying on them.

Lead with one short paragraph the user could say out loud, then the supporting detail."""

MAX_READ_LINES = 400


class DeepAnalyst:
    """Tier 2: Claude with real project tools, answering one question on demand.

    Tier 1 refreshes the panel every few seconds and never leaves the prompt. This runs
    only when the user asks, and is allowed to spend several turns reading the
    repository, because the answer is expected to cite code rather than paraphrase the
    conversation.
    """

    def __init__(
        self,
        settings: Settings,
        root: Path | None = None,
        memory_index: MeetingMemoryIndex | None = None,
        current_meeting_id: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.root = Path(root or project_root()).expanduser().resolve()
        self.project_index = ProjectIndex(self.root)
        self.memory_index = memory_index
        self.current_meeting_id = current_meeting_id
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(
                **anthropic_client_options(self.settings.copilot.deep_timeout_seconds)
            )
        return self._client

    # ---------------------------------------------------------------- tools

    def search_project(self, query: str) -> str:
        """Rank project files against a query and return their paths with excerpts."""
        matches = self.project_index.find_related(query, limit=5)
        if not matches:
            return "No project files matched that query."
        return "\n\n".join(f"--- {m['path']} (score {m['score']})\n{m['excerpt']}" for m in matches)

    def read_project_file(self, path: str, max_lines: int = 200) -> str:
        """Read a bounded window of one project file, refusing anything outside or secret."""
        try:
            target = (self.root / path).resolve()
            target.relative_to(self.root)
        except (ValueError, OSError):
            return f"Refused: {path} resolves outside the project."
        if _is_secret(target):
            return f"Refused: {path} may hold credentials."
        if target.suffix.lower() not in SOURCE_SUFFIXES:
            return f"Refused: {path} is not a readable source file."
        if not target.is_file():
            return f"Not found: {path}"
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            return f"Could not read {path}: {type(exc).__name__}"
        limit = max(1, min(max_lines, MAX_READ_LINES))
        numbered = [f"{number:>5}  {line}" for number, line in enumerate(lines[:limit], start=1)]
        body = "\n".join(numbered)
        if len(lines) > limit:
            body += f"\n... truncated, {len(lines) - limit} more lines in {path}"
        return body

    def git_history(self, path: str = "", limit: int = 10) -> str:
        """Recent commits for the project or one path."""
        command = ["git", "log", "--oneline", "-n", str(max(1, min(limit, 50)))]
        if path:
            command += ["--", path]
        try:
            result = subprocess.run(
                command, cwd=self.root, capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"Git history unavailable: {type(exc).__name__}"
        if result.returncode != 0:
            return f"Git history unavailable: {result.stderr.strip()[:300] or 'not a git repository'}"
        return result.stdout.strip() or "No commits found for that path."

    def find_related_meetings(self, query: str) -> str:
        """Prior meetings on the same subject, from structured memory only."""
        if self.memory_index is None:
            return "No meeting history is available."
        matches = self.memory_index.find_related(query, 3, self.current_meeting_id)
        if not matches:
            return "No prior meeting matched that query."
        return json.dumps(matches, ensure_ascii=False, indent=2)

    def _tools(self) -> list[Any]:
        return [
            beta_tool(self.search_project),
            beta_tool(self.read_project_file),
            beta_tool(self.git_history),
            beta_tool(self.find_related_meetings),
        ]

    # ---------------------------------------------------------------- run

    async def analyze(self, transcript: str, question: str) -> str:
        prompt = (
            f"Recent meeting transcript:\n{transcript}\n\n"
            f"Question: {question}\n"
            f"Answer in: {self.settings.copilot.output_language}."
        )
        runner = self.client.beta.messages.tool_runner(
            model=self.settings.copilot.deep_model,
            max_tokens=4_000,
            max_iterations=self.settings.copilot.deep_max_iterations,
            system=[{"type": "text", "text": DEEP_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=self._tools(),
            messages=[{"role": "user", "content": prompt}],
        )
        message = await runner.until_done()
        return "".join(block.text for block in message.content if block.type == "text").strip()
