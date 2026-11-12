import json
import os
import subprocess
from datetime import datetime

import pytest

from meeting_agent import memory_tasks
from meeting_agent.config import MemoryConfig, Settings
from meeting_agent.listener import _process_started, _write_json
from meeting_agent.memory_tasks import RefreshSchedule, for_chores, onboard_prompt, probe_machine, refresh_prompt
from meeting_agent.working_memory import memory_prompt
from test_command_runner import FakeProvider, _daemon, _feed
from test_listener import _events


def _at(hour, minute, day=12):
    return lambda: datetime(2026, 11, day, hour, minute)


@pytest.mark.parametrize("now,expected", [
    ((8, 31), "08:30"), ((7, 59), ""), ((11, 29), "08:30"), ((11, 31), ""), ((17, 45), "17:30"),
])
def test_a_refresh_is_due_from_its_time_for_a_few_hours(tmp_path, now, expected):
    schedule = RefreshSchedule(["08:30", "17:30"], tmp_path / "state.json", _at(*now))

    assert schedule.due() == expected


def test_each_time_runs_once_a_day_even_across_restarts(tmp_path):
    state = tmp_path / "state.json"

    assert RefreshSchedule(["08:30"], state, _at(8, 31)).due() == "08:30"
    assert RefreshSchedule(["08:30"], state, _at(9, 0)).due() == ""
    assert RefreshSchedule(["08:30"], state, _at(8, 35, day=13)).due() == "08:30"


def test_refresh_times_are_validated_and_sorted():
    assert MemoryConfig(refresh_at=["17:30", "08:30", "08:30"]).refresh_at == ["08:30", "17:30"]
    with pytest.raises(ValueError):
        MemoryConfig(refresh_at=["8:30"])


def test_sources_that_do_not_exist_here_are_left_out(tmp_path):
    (tmp_path / "repos").mkdir()

    memory = MemoryConfig(sources=[str(tmp_path / "repos"), str(tmp_path / "gone")])

    assert memory.source_paths() == [tmp_path / "repos"]


def test_the_memory_prompt_names_the_sources_as_read_only(tmp_path):
    (tmp_path / "memory").mkdir()

    text = memory_prompt(tmp_path / "memory", "buddy-cursor", [tmp_path / "repos"])

    assert f"read and never change: {(tmp_path / 'repos').as_posix()}" in text
    assert "transcripts and screenshots" in text
    assert "read and never change" not in memory_prompt(tmp_path / "memory", "buddy-cursor")


def test_the_probe_names_credential_files_and_never_reads_them(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_tasks, "_logins", lambda: {})
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "work-jira.env").write_text("JIRA_TOKEN=s3cr3t-value\n", encoding="utf-8")
    (tmp_path / ".config" / "theme.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    repo = tmp_path / "Projects" / "shop"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://bot:ghp_abc123@github.com/acme/shop.git"],
                   check=True)
    (repo / "AGENTS.md").write_text("rules", encoding="utf-8")

    probe = probe_machine(tmp_path)
    dumped = json.dumps(probe)

    assert probe["credential_files_names_only"] == [".config/work-jira.env"]
    assert "s3cr3t" not in dumped and "ghp_abc123" not in dumped
    assert probe["agent_folders"] == ["~/.claude/projects"]
    [found] = probe["repositories"]
    assert found["path"] == "~/Projects/shop" and found["instructions"] == ["AGENTS.md"]
    assert found["remote"] == "https://github.com/acme/shop.git"


def test_the_prompts_carry_the_probe_and_where_meetings_are(tmp_path):
    assert '"tools": [\n  "git"\n ]' in onboard_prompt({"tools": ["git"]})
    text = refresh_prompt("08:30", tmp_path / "meetings", "/bin/buddy")
    assert "(08:30)" in text and (tmp_path / "meetings").as_posix() in text and "`/bin/buddy actions --all`" in text


def test_chores_get_the_longest_call_and_the_settings_stay_as_they_were():
    settings = Settings.model_validate({"brain": {"harness": "cursor"}})

    longer = for_chores(settings)

    assert longer.brain.cursor.timeout_seconds == 900 and longer.copilot.claude_timeout_seconds == 900
    assert settings.brain.cursor.timeout_seconds == 240 and settings.copilot.claude_timeout_seconds == 240
    assert longer.brain.harness == "cursor"


async def test_a_chore_runs_while_a_session_follows_and_in_its_own_queue(tmp_path, monkeypatch):
    provider = FakeProvider(replies=["NOTHING"])
    daemon = _daemon(tmp_path, monkeypatch, provider)
    _write_json(daemon.runtime_dir / "brain.json",
                {"session_id": "abc", "pid": os.getpid(), "started": _process_started(os.getpid())})

    daemon._refresh_memory("08:30")
    await daemon._chores._task

    assert daemon._commands is None
    assert "Scheduled memory refresh (08:30)" in provider.prompts[-1][1]
    assert not [r for r in _feed(daemon) if r["type"] in {"REPLY", "BRAIN"}]  # nothing to tell: quiet
    assert any("real state of the work (08:30)" in e.get("text", "") for e in _events(daemon))


async def test_the_listener_runs_the_refresh_when_its_time_comes(tmp_path, monkeypatch):
    provider = FakeProvider(replies=["Moved ITEM-9 to done."])
    daemon = _daemon(tmp_path, monkeypatch, provider)
    daemon._refresh = RefreshSchedule(["08:30"], tmp_path / "state.json", _at(8, 31))

    await daemon.tick()
    await daemon._chores._task
    await daemon.tick()

    assert len(provider.prompts) == 1
    assert [r["text"] for r in _feed(daemon) if r["type"] == "REPLY"] == ["Moved ITEM-9 to done."]


async def test_a_new_memory_is_filled_from_the_machine(tmp_path, monkeypatch, no_real_memory):
    monkeypatch.setattr(memory_tasks, "probe_machine", lambda: {"tools": ["gh"]})
    provider = FakeProvider(replies=["Wrote reference_access.md."])
    daemon = _daemon(tmp_path, monkeypatch, provider)
    daemon._memory_dir = no_real_memory

    await daemon._start_memory()
    await daemon._chores._task

    assert (no_real_memory / "CONVENTIONS.md").exists()
    assert provider.prompts[-1][1].startswith("Onboarding:") and '"gh"' in provider.prompts[-1][1]
    await daemon._start_memory()  # an existing memory is not onboarded again
    assert len(provider.prompts) == 1
