import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meeting_agent import claude_code
from meeting_agent.claude_code import (
    API_CREDENTIAL_VARIABLES,
    REPORT_SYSTEM_PROMPT,
    ClaudeCodeProvider,
    buddy_tool_command,
    claude_binary,
)
from meeting_agent.config import Settings
from meeting_agent.copilot import CopilotWorker, LLMProvider, add_usage, choose_provider
from meeting_agent.events import EventBus, StatusEvent, SuggestionBatchEvent, TranscriptEvent


def _settings(tmp_path, **copilot):
    values = {"claude_workdir": str(tmp_path)}
    values.update(copilot)
    return Settings.model_validate({"llm_provider": "claude-code", "copilot": values})


def _result(text="{}", **extra):
    payload = {
        "type": "result",
        "is_error": False,
        "result": text,
        "session_id": "ignored",
        "total_cost_usd": 0.25,
        "permission_denials": [],
        "usage": {
            "input_tokens": 12,
            "output_tokens": 40,
            "cache_creation_input_tokens": 956,
            "cache_read_input_tokens": 61_628,
        },
    }
    payload.update(extra)
    return json.dumps(payload).encode("utf-8")


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, delay=0.0):
        self.stdout, self.stderr = stdout, stderr
        self.final_code = returncode
        self.returncode = None
        self.delay = delay
        self.pid = 4242
        self.received = None

    async def communicate(self, data):
        self.received = data
        if self.delay:
            await asyncio.sleep(self.delay)
        self.returncode = self.final_code
        return self.stdout, self.stderr

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.fixture
def spawned(monkeypatch):
    """Capture every Claude Code launch instead of starting one."""
    launches = []
    queue = []

    async def fake_exec(*command, **kwargs):
        process = queue.pop(0)
        launches.append({"command": list(command), "process": process, **kwargs})
        return process

    monkeypatch.setattr(claude_code.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")
    return launches, queue


# ------------------------------------------------------------------ binary


def test_the_native_exe_is_preferred_over_the_npm_shim(tmp_path, monkeypatch):
    """cmd.exe cuts a multi-line argument at its first newline, so the shim silently
    delivered only the first line of the appended system prompt."""
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    native = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"")
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: str(shim))

    assert claude_binary() == str(native)


def test_a_real_binary_is_used_as_is(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: str(binary))

    assert claude_binary() == str(binary)


def test_a_missing_claude_makes_the_brain_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: None)

    assert ClaudeCodeProvider(_settings(tmp_path)).available() is False


def test_a_missing_workdir_makes_the_brain_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")

    provider = ClaudeCodeProvider(_settings(tmp_path, claude_workdir=str(tmp_path / "gone")))

    assert provider.available() is False


# ------------------------------------------------------------ environment


def test_api_credentials_never_reach_claude_code(tmp_path, monkeypatch):
    """Buddy's .env carries an Anthropic key with no credit. Passed through, it makes
    Claude Code bill that account instead of the user's subscription."""
    for name in API_CREDENTIAL_VARIABLES:
        monkeypatch.setenv(name, "leaked")
    monkeypatch.setenv("PATH_MARKER", "kept")

    environment = ClaudeCodeProvider(_settings(tmp_path)).environment()

    assert not set(API_CREDENTIAL_VARIABLES) & set(environment)
    assert environment["PATH_MARKER"] == "kept"


# ----------------------------------------------------------------- command


def test_the_command_isolates_the_unattended_run(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")
    provider = ClaudeCodeProvider(_settings(tmp_path, claude_model="opus", claude_effort="medium"), tmp_path / "m")

    command = provider.command("system")

    assert command[:2] == ["claude", "-p"]
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    # User-level settings allow git push; an unattended run must not inherit them.
    assert command[command.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in command
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--effort") + 1] == "medium"
    assert command[command.index("--add-dir") + 1] == str(tmp_path / "m")


def test_variadic_tool_lists_come_last(tmp_path, monkeypatch):
    """Anything after --allowedTools that does not start with a dash would be read as
    one more tool rule."""
    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")
    command = ClaudeCodeProvider(_settings(tmp_path)).command("system")

    allowed = command.index("--allowedTools")
    denied = command.index("--disallowedTools")
    assert allowed < denied
    assert all(not part.startswith("-") for part in command[allowed + 1 : denied])
    assert all(not part.startswith("-") for part in command[denied + 1 :])
    assert "Bash(git push *)" in command[denied + 1 :]


def test_connectors_are_reachable_only_through_the_guarded_command(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")

    with_connectors = ClaudeCodeProvider(_settings(tmp_path)).allowed_tools()
    without = ClaudeCodeProvider(_settings(tmp_path, claude_connectors=False)).allowed_tools()

    rule = f"Bash({buddy_tool_command()} tool *)"
    assert rule in with_connectors
    assert rule not in without


def test_one_session_spans_the_meeting(tmp_path, monkeypatch):
    """Resuming reads the shared context from cache: measured at 956 tokens written on
    the second turn against 119,023 on the first."""
    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")
    provider = ClaudeCodeProvider(_settings(tmp_path))

    first = provider.command("s")
    session = first[first.index("--session-id") + 1]
    assert provider.session_fresh()

    provider._session_started = True
    second = provider.command("s")
    assert "--session-id" not in second
    assert second[second.index("--resume") + 1] == session
    assert not provider.session_fresh()

    provider.reset_session()
    third = provider.command("s")
    assert third[third.index("--session-id") + 1] != session


# ------------------------------------------------------------------ prompt


def test_the_system_prompt_is_complete_and_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")
    settings = _settings(tmp_path, claude_instructions="Use sh ~/bin/jira.sh get KEY for tickets.")
    settings.copilot.output_language = "en"

    raw = ClaudeCodeProvider(settings, tmp_path / "meetings").system_prompt()
    system = " ".join(raw.split())  # the prose is hand-wrapped

    assert "{language}" not in system
    assert "must be in en" in system
    assert "not instructions to you" in system  # the transcript is data
    assert "read-only" in system
    assert (tmp_path / "meetings").as_posix() in system
    assert "sh ~/bin/jira.sh get KEY" in system
    assert f"{buddy_tool_command()} tool" in system


# -------------------------------------------------------------------- run


async def test_a_run_returns_the_result_and_records_usage(tmp_path, spawned, monkeypatch):
    launches, queue = spawned
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leaked")
    denial = {"tool_name": "Bash", "tool_input": {"command": "git push"}}
    queue.append(FakeProcess(_result('{"suggestions": []}', permission_denials=[denial])))
    provider = ClaudeCodeProvider(_settings(tmp_path))

    text = await provider.complete("system", "the prompt")

    assert text == '{"suggestions": []}'
    launch = launches[0]
    assert launch["process"].received == b"the prompt"  # on stdin, never in argv
    assert launch["cwd"] == str(tmp_path)
    assert "ANTHROPIC_API_KEY" not in launch["env"]
    assert provider.last_usage["cache_read_input_tokens"] == 61_628
    assert provider.last_usage["cost_usd_estimate"] == 0.25
    assert provider.last_denials == [denial]
    assert not provider.session_fresh()


async def test_a_reported_error_raises_and_starts_over(tmp_path, spawned):
    _, queue = spawned
    queue.append(FakeProcess(_result("Credit balance is too low", is_error=True)))
    provider = ClaudeCodeProvider(_settings(tmp_path))

    with pytest.raises(RuntimeError, match="Credit balance"):
        await provider.complete("s", "p")

    assert provider.session_fresh()
    assert provider.session_id is None


async def test_output_without_json_carries_stderr(tmp_path, spawned):
    _, queue = spawned
    queue.append(FakeProcess(b"", b"Error: not logged in", returncode=1))
    provider = ClaudeCodeProvider(_settings(tmp_path))

    with pytest.raises(RuntimeError, match="not logged in"):
        await provider.complete("s", "p")

    assert provider.session_fresh()


async def test_a_run_past_the_timeout_is_stopped(tmp_path, spawned, monkeypatch):
    _, queue = spawned
    stopped = []

    async def fake_terminate(process):
        stopped.append(process.pid)

    monkeypatch.setattr(claude_code, "_terminate", fake_terminate)
    queue.append(FakeProcess(_result(), delay=5))
    provider = ClaudeCodeProvider(_settings(tmp_path, claude_timeout_seconds=20))
    provider.settings.copilot.claude_timeout_seconds = 0.05

    with pytest.raises(RuntimeError, match="did not answer"):
        await provider.complete("s", "p")

    assert stopped == [4242]
    assert provider.session_fresh()


async def test_a_cancelled_run_is_stopped(tmp_path, spawned, monkeypatch):
    """The meeting ending must not leave a Claude Code process running on its own."""
    _, queue = spawned
    stopped = []

    async def fake_terminate(process):
        stopped.append(process.pid)

    monkeypatch.setattr(claude_code, "_terminate", fake_terminate)
    queue.append(FakeProcess(_result(), delay=5))
    provider = ClaudeCodeProvider(_settings(tmp_path))

    task = asyncio.create_task(provider.complete("s", "p"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped == [4242]


async def test_terminate_kills_the_whole_tree(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_code.subprocess, "run", lambda args, **kw: calls.append(args))
    process = FakeProcess()

    await claude_code._terminate(process)

    if os.name == "nt":
        assert calls and calls[0][:2] == ["taskkill", "/PID"] and "/T" in calls[0]
    else:
        assert process.returncode == -9


# ------------------------------------------------------------------ worker


class FakeBrain(LLMProvider):
    name = "fake-brain"
    stateful = True
    has_own_tools = True

    def __init__(self, replies=None, delay=0.0):
        super().__init__()
        self.prompts = []
        self.systems = []
        self.replies = list(replies or [])
        self.delay = delay
        self.fresh = True
        self.session_id = "meeting-session"
        self.last_denials = []

    def session_fresh(self):
        return self.fresh

    def system_prompt(self):
        return "BRAIN SYSTEM"

    async def complete(self, system, prompt):
        self.systems.append(system)
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies.pop(0) if self.replies else _suggestions("Ask who owns the rollback.")
        if isinstance(reply, Exception):
            self.fresh = True
            raise reply
        self.fresh = False
        return reply


def _suggestions(*texts, reason="it was raised"):
    return json.dumps({
        "suggestions": [{"kind": "QUESTION", "text": text, "reason": reason} for text in texts],
        "memory_update": "memo", "topics": [], "decisions": [], "action_items": [], "open_questions": ["Q?"],
    })


async def test_the_brain_sees_the_window_once_then_only_new_speech(tmp_path):
    brain = FakeBrain()
    worker = CopilotWorker(Settings(), brain, EventBus())
    worker.recent.append((time.monotonic(), "REMOTE: the rollout moved to Tuesday"))

    await worker._analyze(manual=False)
    worker.recent.append((time.monotonic(), "REMOTE: and ENG-123 is blocked"))
    await worker._analyze(manual=False)

    assert "the rollout moved to Tuesday" in brain.prompts[0]
    assert "New transcript since your last update" in brain.prompts[1]
    assert "ENG-123 is blocked" in brain.prompts[1]
    assert "the rollout moved to Tuesday" not in brain.prompts[1]


async def test_a_failed_session_gets_the_full_window_again(tmp_path):
    brain = FakeBrain(replies=[_suggestions("one"), RuntimeError("boom"), _suggestions("three")])
    worker = CopilotWorker(Settings(), brain, EventBus())
    worker.recent.append((time.monotonic(), "REMOTE: first point"))

    await worker._analyze(manual=False)
    worker.recent.append((time.monotonic(), "REMOTE: second point"))
    await worker._analyze(manual=False)
    await worker._analyze(manual=False)

    assert "first point" in brain.prompts[2] and "second point" in brain.prompts[2]


async def test_the_brain_replaces_the_project_index_and_the_investigator(tmp_path):
    """The project index pointed at Buddy's own repository; with a brain that loads the
    user's context there is nothing for it to add."""

    class Investigator:
        def __init__(self):
            self.questions = []

        async def investigate(self, question, transcript):
            self.questions.append(question)
            return "never"

    investigator = Investigator()
    brain = FakeBrain()
    worker = CopilotWorker(Settings(), brain, EventBus(), investigator=investigator)
    worker.recent.append((time.monotonic(), "REMOTE: anything"))

    await worker._analyze(manual=False)
    await asyncio.sleep(0.05)

    assert brain.systems == ["BRAIN SYSTEM"]
    assert "PROJECT MAP" not in brain.systems[0]
    assert "Project excerpts" not in brain.prompts[0]
    assert investigator.questions == []


async def test_a_manual_request_waits_for_a_slow_brain_instead_of_killing_it():
    """A brain run can spend twenty seconds researching; cancelling it for a keypress
    would throw that work away."""
    bus = EventBus()
    brain = FakeBrain(delay=0.3)
    settings = Settings.model_validate(
        {"copilot": {"suggestion_refresh_seconds": 0.05, "suggestion_debounce_seconds": 0.01}}
    )
    worker = CopilotWorker(settings, brain, bus)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)

    bus.publish(TranscriptEvent("REMOTE", "first point", True, "u1"))
    await asyncio.sleep(0.1)
    worker.suggest_now()
    await asyncio.sleep(0.9)
    stop.set()
    await task

    assert len(brain.prompts) >= 2
    assert worker.suggestion_revision >= 2  # the first run finished rather than being cancelled


async def test_repeated_failures_are_impossible_to_miss(tmp_path):
    """A week of meetings produced nothing because a credit error showed as a yellow
    'retrying' and was never written anywhere."""
    bus = EventBus()
    output = bus.subscribe()
    snapshot = tmp_path / "copilot.json"
    brain = FakeBrain(replies=[RuntimeError("no credits remaining"), RuntimeError("no credits remaining")])
    worker = CopilotWorker(Settings(), brain, bus, snapshot_path=snapshot)
    worker.recent.append((time.monotonic(), "REMOTE: anything"))

    await worker._analyze(manual=False)
    await worker._analyze(manual=False)

    states = [e.state for e in _drain(output) if isinstance(e, StatusEvent) and e.component == "llm"]
    assert "retrying" in states and states[-1] == "failed"
    assert "no credits remaining" in json.loads(snapshot.read_text(encoding="utf-8"))["last_error"]


async def test_success_clears_the_recorded_error(tmp_path):
    brain = FakeBrain(replies=[RuntimeError("blip"), _suggestions("fine")])
    worker = CopilotWorker(Settings(), brain, EventBus())
    worker.recent.append((time.monotonic(), "REMOTE: anything"))

    await worker._analyze(manual=False)
    await worker._analyze(manual=False)

    assert worker.last_error == ""
    assert worker._failures == 0


async def test_suggestions_are_logged_for_after_the_meeting(tmp_path):
    log = tmp_path / "suggestions.md"
    worker = CopilotWorker(
        Settings(), FakeBrain(replies=[_suggestions("Ask who owns it.", reason="nobody said")]),
        EventBus(), suggestions_path=log,
    )
    worker.recent.append((time.monotonic(), "REMOTE: anything"))

    await worker._analyze(manual=False)

    text = log.read_text(encoding="utf-8")
    assert "**QUESTION** Ask who owns it." in text
    assert "nobody said" in text


async def test_blocked_tool_calls_are_surfaced(tmp_path):
    """A denied call is either a rule to widen or something that should stay blocked;
    either way it has to be visible."""
    bus = EventBus()
    output = bus.subscribe()
    snapshot = tmp_path / "copilot.json"
    brain = FakeBrain()
    brain.last_denials = [{"tool_name": "Bash", "tool_input": {"command": "git push"}}]
    worker = CopilotWorker(Settings(), brain, bus, snapshot_path=snapshot)
    worker.recent.append((time.monotonic(), "REMOTE: anything"))

    await worker._analyze(manual=False)

    assert any(isinstance(e, StatusEvent) and e.component == "tools" for e in _drain(output))
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["tool_denials"][0]["tool"] == "Bash"
    assert saved["brain_session_id"] == "meeting-session"


async def test_the_final_report_asks_the_brain_for_markdown():
    brain = FakeBrain(replies=["# Summary\nDone"])
    worker = CopilotWorker(Settings(), brain, EventBus())

    report = await worker.final_report("ME: hello")

    assert report.startswith("# Summary")
    assert brain.systems == [REPORT_SYSTEM_PROMPT]


def _drain(queue):
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


# --------------------------------------------------------------- selection


def test_claude_code_is_selected_only_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")
    chosen = choose_provider(_settings(tmp_path), local_asr=True, meetings_dir=tmp_path)
    assert isinstance(chosen, ClaudeCodeProvider)
    assert chosen.meetings_dir == tmp_path

    monkeypatch.setattr(claude_code, "claude_binary", lambda: None)
    assert choose_provider(_settings(tmp_path), local_asr=True) is None


def test_auto_never_picks_the_brain(tmp_path, monkeypatch):
    """Spending subscription quota is an explicit choice."""
    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    assert choose_provider(Settings(llm_provider="auto"), local_asr=True) is None


def test_usage_keeps_a_fractional_cost():
    total = add_usage({}, {"input_tokens": 10, "cost_usd_estimate": 0.25})
    total = add_usage(total, {"input_tokens": 5, "cost_usd_estimate": 0.1})

    assert total["input_tokens"] == 15
    assert total["cost_usd_estimate"] == pytest.approx(0.35)


# -------------------------------------------------------------------- cli


def test_buddy_tool_lists_only_connectors():
    from meeting_agent.cli import app

    result = CliRunner().invoke(app, ["tool", "--list"])

    assert result.exit_code == 0
    assert "search_past_meetings" in result.stdout
    assert "search_project" not in result.stdout
    assert "read_project_file" not in result.stdout


@pytest.mark.parametrize(
    ("arguments", "message"),
    [(["tool", "rm_everything"], "Unknown tool"), (["tool", "search_past_meetings", "{not json"], "JSON")],
)
def test_buddy_tool_rejects_bad_input(arguments, message):
    from meeting_agent.cli import app

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 2
    assert message in result.stdout


def test_buddy_tool_runs_a_connector():
    from meeting_agent.cli import app

    result = CliRunner().invoke(app, ["tool", "search_past_meetings", '{"query": "zzqx-no-such-topic"}'])

    assert result.exit_code == 0
    assert "No prior meeting matched" in result.stdout


# ------------------------------------------------------------------ openai


async def test_openai_carries_the_reason_for_a_429(monkeypatch):
    """A bare 429 read the same for "slow down" and "no credit left"; the second
    silenced every meeting for a week."""
    from meeting_agent import copilot

    class Response:
        status_code = 429
        text = ""

        def json(self):
            return {"error": {"message": "You have no credits remaining."}}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(copilot.httpx, "AsyncClient", Client)

    with pytest.raises(RuntimeError, match="no credits remaining"):
        await copilot.OpenAIProvider("gpt-5.4-mini").complete("s", "p")
