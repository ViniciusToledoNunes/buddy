from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .claude_code import _terminate, brain_system_prompt
from .config import Settings
from .copilot import LLMProvider


def cursor_agent_binary() -> str | None:
    """The Cursor CLI, which its installer names both cursor-agent and agent.

    A window started from the Dock does not read the shell profile, so the installer's
    directory may be missing from PATH.
    """
    for name in ("cursor-agent", "agent"):
        found = shutil.which(name)
        if found:
            return found
    installed = Path.home() / ".local" / "bin" / "cursor-agent"
    return str(installed) if installed.exists() else None


_MODEL_LINE = re.compile(r"^(\S+) - (.+)$")


def parse_models(output: str) -> list[tuple[str, str]]:
    """`cursor-agent models` prints one `id - Name` line per model between a title and a tip."""
    models = []
    for line in output.splitlines():
        match = _MODEL_LINE.match(line.strip())
        if match:
            models.append((match.group(1), match.group(2).strip()))
    return models


def list_models(timeout: float = 30.0) -> list[tuple[str, str]]:
    binary = cursor_agent_binary()
    if binary is None:
        return []
    try:
        done = subprocess.run([binary, "models"], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_models(done.stdout)


def _result(output: str) -> dict[str, Any]:
    text = output.strip()
    try:
        return json.loads(text)
    except ValueError:
        # Anything printed before the result (a warning, a hook's notice) precedes it.
        return json.loads(text.splitlines()[-1]) if text else {}


class CursorAgentProvider(LLMProvider):
    """Buddy's brain is the user's Cursor agent, run headless.

    It loads the user's Cursor rules, hooks and MCP servers, as a chat in the editor does.
    One chat spans the whole meeting. The CLI has no system-prompt option, so the
    instructions travel in the message: on the chat's first turn, and again whenever they
    change, as when the final report follows the live updates.
    """

    name = "cursor"
    stateful = True
    has_own_tools = True

    def __init__(self, settings: Settings, meetings_dir: Path | None = None) -> None:
        super().__init__()
        self.settings = settings
        self.meetings_dir = meetings_dir
        self.session_id: str | None = None
        self._instructions_sent: str | None = None

    @property
    def workdir(self) -> Path:
        return self.settings.brain.directory()

    def available(self) -> bool:
        return cursor_agent_binary() is not None and self.workdir.is_dir()

    def session_fresh(self) -> bool:
        return self.session_id is None

    def reset_session(self) -> None:
        self.session_id = None
        self._instructions_sent = None

    def system_prompt(self) -> str:
        return brain_system_prompt(self.settings, self.meetings_dir)

    def command(self) -> list[str]:
        brain = self.settings.brain
        command = [
            cursor_agent_binary() or "cursor-agent",
            "-p",
            "--output-format",
            "json",
            "--trust",
            "--workspace",
            str(self.workdir),
        ]
        if self.session_id:
            command += ["--resume", self.session_id]
        if brain.model:
            command += ["--model", brain.model]
        if brain.cursor.force:
            command.append("--force")
        if self.meetings_dir is not None:
            command += ["--add-dir", str(self.meetings_dir)]
        return command

    def message(self, system: str, prompt: str) -> str:
        if system and system != self._instructions_sent:
            return f"<instructions>\n{system}\n</instructions>\n\n{prompt}"
        return prompt

    async def complete(self, system: str, prompt: str) -> str:
        self.last_usage = None
        timeout = self.settings.brain.cursor.timeout_seconds
        message = self.message(system, prompt)
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workdir),
            )
        except OSError as exc:
            self.reset_session()
            raise RuntimeError(f"could not start the Cursor agent: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(message.encode("utf-8")), timeout)
        except asyncio.TimeoutError as exc:
            await _terminate(process)
            self.reset_session()
            raise RuntimeError(f"the Cursor agent did not answer within {timeout:.0f}s") from exc
        except asyncio.CancelledError:
            await _terminate(process)
            raise
        output = stdout.decode("utf-8", "replace")
        try:
            data = _result(output)
        except ValueError:
            data = {}
        if not data:
            self.reset_session()
            detail = (stderr.decode("utf-8", "replace") or output).strip()[:300]
            raise RuntimeError(f"the Cursor agent exited {process.returncode} without a result: {detail}")
        usage = data.get("usage") or {}
        self.last_usage = {
            "input_tokens": int(usage.get("inputTokens", 0) or 0),
            "output_tokens": int(usage.get("outputTokens", 0) or 0),
            "cache_creation_input_tokens": int(usage.get("cacheWriteTokens", 0) or 0),
            "cache_read_input_tokens": int(usage.get("cacheReadTokens", 0) or 0),
        }
        if data.get("is_error"):
            self.reset_session()
            raise RuntimeError(f"the Cursor agent reported an error: {str(data.get('result') or data.get('subtype'))[:300]}")
        self.session_id = str(data.get("session_id") or "") or self.session_id
        self._instructions_sent = system
        return str(data.get("result", ""))
