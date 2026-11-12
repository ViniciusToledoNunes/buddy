import asyncio
import json

import pytest

from meeting_agent import cursor_agent
from meeting_agent.autobrain import AutoBrain
from meeting_agent.claude_code import REPORT_SYSTEM_PROMPT, ClaudeCodeProvider
from meeting_agent.config import Settings
from meeting_agent.copilot import choose_provider
from meeting_agent.cursor_agent import CursorAgentProvider, cursor_agent_binary
from meeting_agent.doctor import brain_check
from meeting_agent.harness import CursorHarness, harness_for

# What cursor-agent 2026.11.11 printed for the two turns of one chat.
FIRST_TURN = {
    "type": "result", "subtype": "success", "is_error": False, "duration_ms": 2628,
    "duration_api_ms": 2628, "result": "first OK", "session_id": "dfd7659f-7584-4e42-b03d-cbab94c271c9",
    "request_id": "a926bf6b-648b-47bb-8b1b-1f2f32849ed3",
    "usage": {"inputTokens": 12083, "outputTokens": 39, "cacheReadTokens": 4160, "cacheWriteTokens": 0},
}
SECOND_TURN = {
    **FIRST_TURN, "result": "PINEAPPLE", "request_id": "af029562-ed31-4e35-97da-8d2d0187a6ed",
    "usage": {"inputTokens": 77, "outputTokens": 53, "cacheReadTokens": 16256, "cacheWriteTokens": 0},
}
UNKNOWN_MODEL = b"Cannot use this model: no-such-model-xyz. Available models: auto, composer-2.5"


def _settings(tmp_path, **brain):
    values = {"harness": "cursor", "workdir": str(tmp_path), **brain}
    return Settings.model_validate({"llm_provider": "harness", "brain": values})


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, delay=0.0):
        self.stdout, self.stderr = stdout, stderr
        self.final_code = returncode
        self.returncode = None
        self.delay = delay
        self.pid = 4343
        self.received = None

    async def communicate(self, data):
        self.received = data.decode("utf-8")
        if self.delay:
            await asyncio.sleep(self.delay)
        self.returncode = self.final_code
        return self.stdout, self.stderr


def _turn(payload):
    return FakeProcess(json.dumps(payload).encode("utf-8"))


@pytest.fixture
def spawned(monkeypatch):
    """Capture every Cursor agent launch instead of starting one."""
    launches = []
    queue = []

    async def fake_exec(*command, **kwargs):
        process = queue.pop(0)
        launches.append({"command": list(command), "process": process, **kwargs})
        return process

    monkeypatch.setattr(cursor_agent.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(cursor_agent, "cursor_agent_binary", lambda: "cursor-agent")
    return launches, queue


def test_the_brain_block_switches_between_claude_code_and_cursor(tmp_path):
    cursor = _settings(tmp_path)
    claude = _settings(tmp_path, harness="claude-code")

    assert isinstance(harness_for(cursor), CursorHarness)
    assert isinstance(harness_for(cursor).provider(cursor), CursorAgentProvider)
    assert isinstance(harness_for(claude).provider(claude), ClaudeCodeProvider)
    assert isinstance(AutoBrain(cursor, tmp_path / "meetings").provider, CursorAgentProvider)


def test_llm_provider_harness_runs_cursor_when_its_cli_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor_agent, "cursor_agent_binary", lambda: "cursor-agent")
    assert isinstance(choose_provider(_settings(tmp_path), local_asr=True), CursorAgentProvider)

    monkeypatch.setattr(cursor_agent, "cursor_agent_binary", lambda: None)
    assert choose_provider(_settings(tmp_path), local_asr=True) is None


def test_the_doctor_names_what_cursor_needs(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor_agent, "cursor_agent_binary", lambda: None)
    assert brain_check(_settings(tmp_path)).state == "failed"

    monkeypatch.setattr(cursor_agent, "cursor_agent_binary", lambda: "cursor-agent")
    ready = brain_check(_settings(tmp_path, model="composer-2.5"))
    assert ready.state == "ok" and "composer-2.5" in ready.detail


def test_the_installers_cursor_agent_is_found_outside_path(tmp_path, monkeypatch):
    """A window started from the Dock does not read the shell profile."""
    installed = tmp_path / ".local" / "bin" / "cursor-agent"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"")
    monkeypatch.setattr(cursor_agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(cursor_agent.Path, "home", lambda: tmp_path)

    assert cursor_agent_binary() == str(installed)


def test_the_first_turn_starts_a_chat_and_the_next_resumes_it(tmp_path, spawned):
    launches, queue = spawned
    queue += [_turn(FIRST_TURN), _turn(SECOND_TURN)]
    provider = CursorAgentProvider(_settings(tmp_path, model="composer-2.5"), tmp_path / "meetings")

    assert asyncio.run(provider.complete("be the copilot", "speech one")) == "first OK"
    assert asyncio.run(provider.complete("be the copilot", "speech two")) == "PINEAPPLE"

    first, second = launches[0]["command"], launches[1]["command"]
    assert first[:3] == ["cursor-agent", "-p", "--output-format"]
    assert first[first.index("--workspace") + 1] == str(tmp_path)
    assert first[first.index("--model") + 1] == "composer-2.5"
    assert first[first.index("--add-dir") + 1] == str(tmp_path / "meetings")
    assert "--force" in first and "--trust" in first
    assert "--resume" not in first
    assert second[second.index("--resume") + 1] == FIRST_TURN["session_id"]
    assert launches[0]["cwd"] == str(tmp_path)


def test_the_instructions_travel_once_and_again_when_they_change(tmp_path, spawned):
    """The CLI has no system-prompt option; the report follows with instructions of its own."""
    launches, queue = spawned
    queue += [_turn(FIRST_TURN), _turn(SECOND_TURN), _turn(SECOND_TURN)]
    provider = CursorAgentProvider(_settings(tmp_path))

    asyncio.run(provider.complete("be the copilot", "speech one"))
    asyncio.run(provider.complete("be the copilot", "speech two"))
    asyncio.run(provider.complete(REPORT_SYSTEM_PROMPT, "write the report"))

    sent = [launch["process"].received for launch in launches]
    assert sent[0].startswith("<instructions>\nbe the copilot\n</instructions>") and sent[0].endswith("speech one")
    assert sent[1] == "speech two"
    assert REPORT_SYSTEM_PROMPT in sent[2] and sent[2].endswith("write the report")


def test_the_usage_is_read_from_cursors_names(tmp_path, spawned):
    _, queue = spawned
    queue.append(_turn(SECOND_TURN))
    provider = CursorAgentProvider(_settings(tmp_path))
    asyncio.run(provider.complete("s", "p"))

    assert provider.last_usage == {
        "input_tokens": 77,
        "output_tokens": 53,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 16256,
    }


def test_force_can_be_turned_off_and_no_model_means_cursors_default(tmp_path, spawned):
    launches, queue = spawned
    queue.append(_turn(FIRST_TURN))
    provider = CursorAgentProvider(_settings(tmp_path, cursor={"force": False}))
    asyncio.run(provider.complete("s", "p"))

    command = launches[0]["command"]
    assert "--force" not in command and "--model" not in command and "--add-dir" not in command


def test_a_refused_model_fails_with_cursors_reason_and_a_fresh_chat_follows(tmp_path, spawned):
    launches, queue = spawned
    queue += [_turn(FIRST_TURN), FakeProcess(b"", UNKNOWN_MODEL, returncode=1), _turn(FIRST_TURN)]
    provider = CursorAgentProvider(_settings(tmp_path))
    asyncio.run(provider.complete("s", "p"))

    with pytest.raises(RuntimeError, match="Cannot use this model"):
        asyncio.run(provider.complete("s", "p"))
    assert provider.session_fresh()

    asyncio.run(provider.complete("s", "p"))
    assert "--resume" not in launches[2]["command"]
    assert launches[2]["process"].received.startswith("<instructions>")


def test_a_reported_error_fails_the_turn(tmp_path, spawned):
    _, queue = spawned
    queue.append(_turn({**FIRST_TURN, "is_error": True, "result": "rate limited"}))
    provider = CursorAgentProvider(_settings(tmp_path))

    with pytest.raises(RuntimeError, match="rate limited"):
        asyncio.run(provider.complete("s", "p"))
    assert provider.session_fresh()


def test_a_notice_printed_before_the_result_is_skipped(tmp_path, spawned):
    _, queue = spawned
    queue.append(FakeProcess(b"warning: something\n" + json.dumps(FIRST_TURN).encode("utf-8")))
    assert asyncio.run(CursorAgentProvider(_settings(tmp_path)).complete("s", "p")) == "first OK"


def test_a_run_past_the_timeout_is_stopped(tmp_path, spawned, monkeypatch):
    _, queue = spawned
    stopped = []

    async def fake_terminate(process):
        stopped.append(process.pid)

    monkeypatch.setattr(cursor_agent, "_terminate", fake_terminate)
    queue.append(FakeProcess(json.dumps(FIRST_TURN).encode("utf-8"), delay=5))
    provider = CursorAgentProvider(_settings(tmp_path))
    provider.settings.brain.cursor.timeout_seconds = 0.05

    with pytest.raises(RuntimeError, match="did not answer"):
        asyncio.run(provider.complete("s", "p"))
    assert stopped == [4343]
    assert provider.session_fresh()
