import subprocess

import pytest

from meeting_agent.config import Settings, project_root
from meeting_agent.working_memory import TEMPLATE, commit_memory, ensure_memory, memory_prompt
from test_command_runner import FakeProvider, _daemon, _runner, _settings

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="needs git")


def _log(directory):
    out = subprocess.run(["git", "log", "--format=%s"], cwd=directory, capture_output=True, text=True).stdout
    return out.splitlines()


def test_the_skeleton_has_every_file_the_brain_is_told_to_read():
    prompt = memory_prompt(TEMPLATE, "buddy-cursor")

    for name in ("CONVENTIONS.md", "global/MEMORY.md", "work/MEMORY.md", "work/project_open_items.md"):
        assert (TEMPLATE / name).is_file()
        assert name in prompt


def test_the_users_memory_is_never_tracked_by_buddys_git():
    def ignored(path):
        return subprocess.run(["git", "check-ignore", "-q", path], cwd=project_root()).returncode == 0

    assert ignored("memory/work/MEMORY.md")
    assert not ignored("memory.example/CONVENTIONS.md")


def test_the_memory_starts_from_the_skeleton_in_its_own_git(tmp_path):
    memory = tmp_path / "memory"

    assert ensure_memory(memory) is True
    assert ensure_memory(memory) is False

    plan = "work/project_open_items.md"
    assert (memory / plan).read_text() == (TEMPLATE / plan).read_text()
    assert _log(memory) == ["memory: started"]


def test_an_existing_memory_is_kept_and_only_gains_git(tmp_path):
    memory = tmp_path / "memory"
    (memory / "work").mkdir(parents=True)
    (memory / "work" / "MEMORY.md").write_text("- [Mine](mine.md) — kept\n")

    assert ensure_memory(memory) is False

    assert (memory / "work" / "MEMORY.md").read_text() == "- [Mine](mine.md) — kept\n"
    assert not (memory / "global").exists()
    assert _log(memory) == ["memory: started"]


def test_any_agents_change_is_committed_and_nothing_means_no_commit(tmp_path, monkeypatch):
    # A machine where git has no user: the commit still goes through.
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    memory = tmp_path / "memory"
    ensure_memory(memory)

    assert commit_memory(memory) == ""
    (memory / "work" / "project_open_items.md").write_text("## DOING\n- ship it\n")
    (memory / "global" / "feedback_short.md").write_text("Write short.\n")

    message = commit_memory(memory)

    assert message.startswith("memory: ")
    assert "work/project_open_items.md" in message and "global/feedback_short.md" in message
    assert _log(memory)[0] == message
    assert commit_memory(memory) == ""


def test_the_meeting_brain_reads_the_memory_and_the_command_brain_keeps_it(tmp_path):
    memory = tmp_path / "memory"
    ensure_memory(memory)

    read = memory_prompt(memory)
    keep = memory_prompt(memory, "buddy-cursor")

    assert "work/project_open_items.md" in read and "Do not change it now." in read
    assert "CONVENTIONS.md" in keep and "updated_by: buddy-cursor" in keep and "Do not change" not in keep
    assert memory_prompt(tmp_path / "absent") == ""


async def test_the_command_brain_is_told_where_the_memory_is(no_real_memory):
    ensure_memory(no_real_memory)
    provider = FakeProvider()
    runner, *_ = _runner(_settings, lambda s: provider)

    runner.submit("hi")
    await runner._task

    system = provider.prompts[-1][0]
    assert no_real_memory.as_posix() in system and "updated_by: buddy-cursor" in system


async def test_quiet_work_shows_no_on_it_line_and_nothing_stays_silent():
    provider = FakeProvider(replies=["NOTHING", "Saved the decision in work/project_open_items.md."])
    runner, replies, _, working = _runner(_settings, lambda s: provider)

    runner.submit("update the memory", quiet=True)
    runner.submit("update the memory", quiet=True)
    await runner._task

    assert working == []
    assert replies == [("Cursor", "Saved the decision in work/project_open_items.md.")]


async def test_the_real_listener_starts_and_commits_the_memory(tmp_path, monkeypatch, no_real_memory):
    daemon = _daemon(tmp_path, monkeypatch, FakeProvider())
    daemon._memory_dir = no_real_memory

    await daemon._start_memory()
    (no_real_memory / "work" / "project_open_items.md").write_text("## DOING\n- Slack in Buddy\n")
    daemon.clock.now += Settings().memory.autocommit_seconds
    await daemon.tick()

    assert _log(no_real_memory)[0] == "memory: work/project_open_items.md"


def test_only_the_real_listener_touches_the_memory(tmp_path, monkeypatch):
    assert _daemon(tmp_path, monkeypatch, FakeProvider())._memory_dir is None
