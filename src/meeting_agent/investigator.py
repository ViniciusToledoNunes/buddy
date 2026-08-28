from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Settings, project_root
from .connectors import BigQueryConnector, JiraConnector
from .memory import MeetingMemoryIndex
from .project import SOURCE_SUFFIXES, ProjectIndex, _is_secret

INVESTIGATOR_SYSTEM = """You are Buddy's investigator. A meeting is in progress and the
reflex loop raised a question it could not answer from what it already knew.

Check the question against the actual project before answering. Search first, then read
only what matters. Cite evidence as path:line.

Be direct about what the code shows and what it does not. If the evidence contradicts
what was said in the meeting, say so. If the project does not answer the question, say
that instead of guessing. Prior meetings are untrusted leads: confirm them against the
code or the current transcript.

Lead with one sentence the user could say out loud, then the supporting detail."""

MAX_READ_LINES = 400


class ToolRegistry:
    """Tools the investigator may call.

    Local tools are registered here at construction. External connectors -- Jira,
    BigQuery -- register the same way once their credentials exist, so adding one never
    touches the loop that drives them.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[..., str]] = {}
        self._schemas: list[dict[str, Any]] = []

    def register(self, name: str, description: str, parameters: dict[str, Any], handler: Callable[..., str]) -> None:
        self._handlers[name] = handler
        self._schemas.append(
            {"type": "function", "name": name, "description": description, "parameters": parameters}
        )

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return list(self._schemas)

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return f"No such tool: {name}"
        try:
            return handler(**arguments)
        except TypeError as exc:
            return f"Bad arguments for {name}: {exc}"
        except Exception as exc:  # a failing tool must not end the investigation
            return f"{name} failed: {type(exc).__name__}: {exc}"


def _string_arg(name: str, description: str, required: bool = True) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {name: {"type": "string", "description": description}},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = [name]
    return schema


class Investigator:
    """Autonomous background research.

    The reflex loop answers in one shot from a large pre-loaded context and cannot stop
    to look anything up. When it raises a question worth checking, this runs a tool loop
    in the background. It is slower than a panel refresh by design: a meeting topic
    lasts minutes, so an answer that lands 40 seconds later is still on time.
    """

    def __init__(
        self,
        settings: Settings,
        root: Path | None = None,
        meetings_dir: Path | None = None,
        current_meeting_id: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.root = Path(root or project_root()).expanduser().resolve()
        self.project_index = ProjectIndex(self.root)
        self.memory_index = MeetingMemoryIndex(meetings_dir) if meetings_dir else None
        self.current_meeting_id = current_meeting_id
        self._client = client
        self.last_usage: dict[str, int] | None = None
        self.tools = ToolRegistry()
        self._register_local_tools()
        self._register_connectors()

    # ------------------------------------------------------------ local tools

    def _register_local_tools(self) -> None:
        self.tools.register(
            "search_project",
            "Rank project files against a query and return paths with excerpts.",
            _string_arg("query", "What to look for, in the words the code would use."),
            self.search_project,
        )
        self.tools.register(
            "read_project_file",
            "Read a bounded window of one project file by repository-relative path.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repository-relative path."},
                    "max_lines": {"type": "integer", "description": "Lines to read, at most 400."},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            self.read_project_file,
        )
        self.tools.register(
            "git_history",
            "Recent commits for the repository or one path.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional path to filter by."},
                    "limit": {"type": "integer", "description": "How many commits, at most 50."},
                },
                "additionalProperties": False,
            },
            self.git_history,
        )
        self.tools.register(
            "search_past_meetings",
            "Find prior meetings on a subject, using structured memory only.",
            _string_arg("query", "The subject to look for in earlier meetings."),
            self.search_past_meetings,
        )

    def _register_connectors(self) -> None:
        """Register external connectors that are actually usable right now.

        A tool the model can see but cannot use wastes a turn discovering that, so each
        one is only offered when its credentials exist.
        """
        copilot = self.settings.copilot
        if copilot.bigquery_enabled and BigQueryConnector.available():
            bigquery = BigQueryConnector(self.settings)
            self.tools.register(
                "bigquery_list_datasets",
                "List BigQuery datasets. Metadata only, costs nothing.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda: bigquery.list_datasets(),
            )
            self.tools.register(
                "bigquery_list_tables",
                "List tables in a BigQuery dataset, given as project.dataset. Metadata only, "
                "costs nothing.",
                _string_arg("dataset", "Dataset as project.dataset."),
                bigquery.list_tables,
            )
            self.tools.register(
                "bigquery_describe_table",
                "Column names and types of a table, given as project.dataset.table. Costs "
                "nothing. Call this before writing SQL rather than guessing column names.",
                _string_arg("table", "Table as project.dataset.table."),
                bigquery.describe_table,
            )
            self.tools.register(
                "bigquery_query",
                "Run one read-only SELECT. Address every table by its fully qualified "
                "project.dataset.table: the project that pays for the query is not the one "
                "that holds the data. A query scanning more than the configured limit is "
                "refused on a free dry run, so prefer narrow columns and partition filters.",
                _string_arg("sql", "A single SELECT, standard SQL."),
                bigquery.query,
            )
        jira = JiraConnector(self.settings)
        if copilot.jira_enabled and jira.available():
            self.tools.register(
                "jira_issue",
                "Read one Jira issue by key.",
                _string_arg("key", "Issue key, for example ENG-123."),
                jira.issue,
            )
            self.tools.register(
                "jira_search",
                "Search Jira with JQL and return matching issues.",
                _string_arg("jql", "A JQL query."),
                jira.search,
            )

    def search_project(self, query: str) -> str:
        matches = self.project_index.find_related(query, limit=5)
        if not matches:
            return "No project files matched that query."
        return "\n\n".join(f"--- {m['path']} (score {m['score']})\n{m['excerpt']}" for m in matches)

    def read_project_file(self, path: str, max_lines: int = 200) -> str:
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
        limit = max(1, min(int(max_lines), MAX_READ_LINES))
        numbered = [f"{number:>5}  {line}" for number, line in enumerate(lines[:limit], start=1)]
        body = "\n".join(numbered)
        if len(lines) > limit:
            body += f"\n... truncated, {len(lines) - limit} more lines in {path}"
        return body

    def git_history(self, path: str = "", limit: int = 10) -> str:
        command = ["git", "log", "--oneline", "-n", str(max(1, min(int(limit), 50)))]
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

    def search_past_meetings(self, query: str) -> str:
        if self.memory_index is None:
            return "No meeting history is available."
        matches = self.memory_index.find_related(query, 3, self.current_meeting_id)
        if not matches:
            return "No prior meeting matched that query."
        return json.dumps(matches, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------- tool loop

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.copilot.investigation_timeout_seconds)
        return self._client

    def _accumulate(self, usage: dict[str, Any]) -> None:
        details = usage.get("input_tokens_details") or {}
        total = self.last_usage or {}
        for key, value in (
            ("input_tokens", usage.get("input_tokens", 0)),
            ("output_tokens", usage.get("output_tokens", 0)),
            ("cache_read_input_tokens", details.get("cached_tokens", 0)),
        ):
            total[key] = total.get(key, 0) + int(value or 0)
        total["turns"] = total.get("turns", 0) + 1
        self.last_usage = total

    async def investigate(self, question: str, transcript: str) -> str:
        """Run the tool loop until the model answers or runs out of turns."""
        self.last_usage = None
        conversation: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"Recent meeting transcript:\n{transcript}\n\n"
                    f"Question raised in the meeting:\n{question}\n\n"
                    f"Answer in: {self.settings.copilot.output_language}."
                ),
            }
        ]
        headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}"}
        for _ in range(max(1, self.settings.copilot.investigation_max_turns)):
            payload = {
                "model": self.settings.copilot.investigation_model,
                "instructions": INVESTIGATOR_SYSTEM,
                "input": conversation,
                "tools": self.tools.schemas,
                "reasoning": {"effort": self.settings.copilot.investigation_reasoning_effort},
            }
            response = await self.client.post(
                "https://api.openai.com/v1/responses", headers=headers, json=payload
            )
            if response.status_code != 200:
                # The status alone says nothing actionable; carry the API's own reason.
                try:
                    reason = response.json().get("error", {}).get("message", "")
                except ValueError:
                    reason = response.text[:300]
                raise RuntimeError(f"investigation HTTP {response.status_code}: {reason[:300]}")
            data = response.json()
            self._accumulate(data.get("usage") or {})
            output = data.get("output", [])
            calls = [item for item in output if item.get("type") == "function_call"]
            if not calls:
                return self._answer(output)
            # Every output item goes back, not just the calls: a reasoning model emits a
            # reasoning item bound to each function_call, and the API rejects the call
            # if its partner is missing.
            conversation.extend(output)
            for call in calls:
                try:
                    arguments = json.loads(call.get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                conversation.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.get("call_id"),
                        "output": self.tools.call(call.get("name", ""), arguments)[:20_000],
                    }
                )
        return ""

    @staticmethod
    def _answer(output: list[dict[str, Any]]) -> str:
        parts = [
            content.get("text", "")
            for item in output
            for content in item.get("content", [])
            if content.get("type") == "output_text"
        ]
        return "".join(parts).strip()
