import json
import os

import pytest

from meeting_agent.autobrain import AutoBrain, CommandRunner, command_system_prompt
from meeting_agent.config import Settings
from meeting_agent.copilot import LLMProvider
from meeting_agent.listener import ListenDaemon, _process_started, _write_json
from meeting_agent.live import LiveItem, item_from_feed, terminal_line
from test_listener import Clock, FakeStream, _events


class FakeProvider(LLMProvider):
    """Stands in for the harness CLI. Never runs anything."""

    def __init__(self, replies=None, fail=False, available=True):
        super().__init__()
        self.prompts = []
        self.replies = list(replies or [])
        self.fail = fail
        self._available = available

    def available(self):
        return self._available

    async def complete(self, system, prompt):
        self.prompts.append((system, prompt))
        if self.fail:
            raise RuntimeError("boom")
        return self.replies.pop(0) if self.replies else "Done."


def _settings(harness="cursor", model=""):
    return Settings.model_validate({"brain": {"harness": harness, "model": model}})


def _runner(settings, provider_factory):
    replies, notices, working = [], [], []
    runner = CommandRunner(
        settings, None,
        reply=lambda label, text: replies.append((label, text)),
        notice=notices.append,
        working=working.append,
        provider_factory=provider_factory,
    )
    return runner, replies, notices, working


def test_the_command_prompt_takes_the_skills_rules_with_the_real_command(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "# Buddy\n\n## Events\nwatch only\n\n## Action cards\nPropose with `BUDDY_COMMAND propose`.\n\n"
        "## Boundaries\nNever post without approval.\n",
        encoding="utf-8",
    )

    prompt = command_system_prompt("/bin/buddy", skill)

    assert "Propose with `/bin/buddy propose`." in prompt
    assert "Never post without approval." in prompt
    assert "watch only" not in prompt
    assert "BUDDY_COMMAND" not in prompt


def test_the_shipped_skill_has_every_section_the_command_prompt_needs():
    prompt = command_system_prompt("buddy")

    for title in ("Action cards", "Work board", "Commands", "Boundaries"):
        assert f"## {title}\n" in prompt


async def test_commands_are_answered_in_order_and_named_after_the_harness():
    provider = FakeProvider(replies=["First.", "Second."])
    runner, replies, notices, working = _runner(_settings, lambda s: provider)

    runner.submit("one")
    runner.submit("two")
    await runner._task

    assert [prompt for _, prompt in provider.prompts] == ["one", "two"]
    assert replies == [("Cursor", "First."), ("Cursor", "Second.")]
    assert working == ["Cursor", "Cursor"] and notices == []


async def test_one_conversation_until_the_harness_or_model_changes():
    choice = {"harness": "cursor", "model": ""}
    built = []

    def factory(settings):
        built.append((settings.brain.harness, settings.brain.model))
        return FakeProvider()

    runner, replies, _, _ = _runner(lambda: _settings(**choice), factory)

    runner.submit("a")
    runner.submit("b")
    await runner._task
    choice["model"] = "gpt-5"
    runner.submit("c")
    await runner._task
    choice["harness"] = "claude-code"
    runner.submit("d")
    await runner._task

    assert built == [("cursor", ""), ("cursor", "gpt-5"), ("claude-code", "gpt-5")]
    assert replies[-1][0] == "Claude Code"


async def test_a_failed_call_is_a_notice_and_the_next_command_still_runs():
    providers = iter([FakeProvider(fail=True)])
    runner, replies, notices, _ = _runner(_settings, lambda s: next(providers))

    runner.submit("one")
    await runner._task
    runner.provider.fail = False
    runner.submit("two")
    await runner._task

    assert notices == ["Cursor could not answer: RuntimeError: boom"]
    assert replies == [("Cursor", "Done.")]


async def test_an_unavailable_harness_says_nobody_can_answer():
    runner, replies, notices, working = _runner(_settings, lambda s: FakeProvider(available=False))

    runner.submit("one")
    await runner._task

    assert replies == [] and working == []
    assert "Cursor is not available here" in notices[0]


def _daemon(tmp_path, monkeypatch, provider, answer_commands=True, **listen):
    listen.setdefault("sounds", False)
    settings = Settings.model_validate({
        "meetings_dir": str(tmp_path / "meetings"), "listen": listen, "brain": {"harness": "cursor"},
    })
    monkeypatch.setattr(CommandRunner, "_harness_provider", lambda self, s: provider)
    daemon = ListenDaemon(
        settings, tmp_path / "runtime", stream_factory=lambda s: FakeStream(s, []), clock=Clock(),
        brain_factory=lambda: AutoBrain(settings, provider_factory=lambda: provider),
        answer_commands=answer_commands,
    )
    daemon.chime = lambda kind: None
    daemon.claim()
    return daemon


def _feed(daemon):
    path = daemon.runtime_dir / "live.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


async def test_a_typed_command_with_no_session_watching_is_answered_in_the_window(tmp_path, monkeypatch):
    provider = FakeProvider(replies=["Daily: shipped ITEM-1."])
    daemon = _daemon(tmp_path, monkeypatch, provider)

    await daemon.typed("prepara o report da daily")
    await daemon._commands._task

    assert provider.prompts[-1][1] == 'The user typed in Buddy\'s window: "prepara o report da daily"'
    reply = [r for r in _feed(daemon) if r["type"] == "REPLY"]
    assert reply[-1]["by"] == "Cursor" and reply[-1]["text"] == "Daily: shipped ITEM-1."
    assert any(r["type"] == "BRAIN" and r["text"] == "Cursor is on it…" for r in _feed(daemon))
    assert any(e["type"] == "COMMAND" for e in _events(daemon))  # a session that comes later still sees it


async def test_an_approved_card_goes_to_the_built_in_brain_as_approved(tmp_path, monkeypatch):
    from meeting_agent.actions import ActionStore

    provider = FakeProvider()
    daemon = _daemon(tmp_path, monkeypatch, provider)
    store = ActionStore(daemon.runtime_dir)
    card = store.propose("ticket", "Open ITEM-9", detail="Title: fix the brand field")
    store.decide(card.id, approve=True)

    daemon._announce_action(card.id)
    await daemon._commands._task

    prompt = provider.prompts[-1][1]
    assert f"ACTION_APPROVED id={card.id}" in prompt and "Title: fix the brand field" in prompt


@pytest.mark.parametrize("case", ["session watching", "auto brain off", "not the real listener"])
async def test_the_built_in_brain_stays_out_when_it_should(tmp_path, monkeypatch, case):
    provider = FakeProvider()
    daemon = _daemon(
        tmp_path, monkeypatch, provider,
        answer_commands=case != "not the real listener", auto_brain=case != "auto brain off",
    )
    if case == "session watching":
        _write_json(daemon.runtime_dir / "brain.json",
                    {"session_id": "abc", "pid": os.getpid(), "started": _process_started(os.getpid())})

    await daemon.typed("prepara o report da daily")

    assert daemon._commands is None and provider.prompts == []


def test_a_reply_shows_under_the_harness_name():
    item = item_from_feed({"type": "REPLY", "by": "Cursor", "text": "Done.", "ts": 1})

    assert item == LiveItem("claude", "Done.", who="Cursor", at=item.at)
    assert "Cursor Done." in terminal_line(item)
