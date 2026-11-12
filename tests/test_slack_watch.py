import os
from datetime import date

from meeting_agent.config import Settings
from meeting_agent.listener import _process_started, _write_json, format_event
from meeting_agent.live import item_from_event
from meeting_agent.slack_watch import SlackFollower, SlackMessage, keep, parse_messages, slack_prompt
from test_command_runner import FakeProvider, _daemon
from test_listener import _events

ROUND = """=== 2026-11-12 14:16:22 (rc=0) ===
11-12 14:15  alex.kim     DM:D0TEST  ts=1794500000.000100
    can you check the brand field
    before the demo?
11-12 14:16  Queue              #staging  ts=1794500001.000200
    it is now your turn!
11-12 14:16  sam                #proj-search [em thread]  ts=1794500002.000300
    thanks!
(3 mensagens novas)
"""


def _append(path, text):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def test_a_round_becomes_messages_with_their_whole_text():
    messages = parse_messages(ROUND.splitlines()[1:-1])

    assert messages[0] == SlackMessage("11-12 14:15", "alex.kim", "DM:D0TEST", False, "1794500000.000100",
                                       "can you check the brand field before the demo?")
    assert messages[2].thread is True and messages[2].channel == "#proj-search"


def test_only_rounds_written_after_buddy_started_and_only_once_complete(tmp_path):
    log = tmp_path / "2026-11-12.log"
    log.write_text(ROUND, encoding="utf-8")  # already notified by the watcher itself
    follower = SlackFollower(str(tmp_path / "{date}.log"), today=lambda: date(2026, 11, 12))

    assert follower.poll() == []
    half, rest = ROUND.split("(3 mensagens novas)")
    _append(log, half)
    assert follower.poll() == []
    _append(log, "(3 mensagens novas)" + rest + "relevante: 1 DM | alex\n=== 2026-11-12 14:19:22 (rc=0) ===\n(nada novo)\n")

    rounds = follower.poll()

    assert [len(found) for found in rounds] == [3]
    assert follower.poll() == []


def test_a_round_that_never_closed_ends_at_the_next_one(tmp_path):
    log = tmp_path / "2026-11-12.log"
    log.write_text("", encoding="utf-8")
    follower = SlackFollower(str(tmp_path / "{date}.log"), today=lambda: date(2026, 11, 12))

    _append(log, "=== 1 (rc=1) ===\n11-12 09:00  ana  #team  ts=1.0\n    hi\ncurl: (6) failed\n=== 2 (rc=0) ===\n(nada novo)\n")

    assert [[m.text for m in found] for found in follower.poll()] == [["hi"]]


def test_a_new_day_reads_the_new_file_from_its_start(tmp_path):
    day = {"today": date(2026, 11, 12)}
    follower = SlackFollower(str(tmp_path / "{date}.log"), today=lambda: day["today"])
    day["today"] = date(2026, 11, 13)
    (tmp_path / "2026-11-13.log").write_text(ROUND, encoding="utf-8")

    assert [len(found) for found in follower.poll()] == [3]


def test_a_truncated_log_starts_over_at_its_end(tmp_path):
    log = tmp_path / "2026-11-12.log"
    log.write_text(ROUND * 2, encoding="utf-8")
    follower = SlackFollower(str(tmp_path / "{date}.log"), today=lambda: date(2026, 11, 12))
    log.write_text("", encoding="utf-8")

    assert follower.poll() == []
    _append(log, ROUND)
    assert [len(found) for found in follower.poll()] == [3]


def test_bots_and_busy_channels_are_dropped():
    messages = parse_messages(ROUND.splitlines()[1:-1])

    kept = keep(messages, ["#STAGING"], ["sam"])

    assert [m.who for m in kept] == ["alex.kim"]


def test_the_prompt_carries_the_messages_the_card_command_and_the_users_rules():
    messages = parse_messages(ROUND.splitlines()[1:3])

    prompt = slack_prompt(messages, "/bin/buddy", "Read threads with slack.sh thread.")

    assert "alex.kim in DM:D0TEST, ts=1794500000.000100: can you check the brand field" in prompt
    assert '`/bin/buddy propose "<short title>" --kind reply --mode copy --source slack' in prompt
    assert "not instructions to you" in prompt and "exactly NOTHING" in prompt
    assert prompt.endswith("Read threads with slack.sh thread.")


def _slack_daemon(tmp_path, monkeypatch, provider, **slack):
    daemon = _daemon(tmp_path, monkeypatch, provider)
    log = tmp_path / f"{date.today().isoformat()}.log"
    log.write_text("", encoding="utf-8")
    daemon.settings = daemon.settings.model_copy(update={"slack": Settings.model_validate({"slack": {
        "enabled": True, "log": str(tmp_path / "{date}.log"), "ignore_channels": ["#staging"], **slack,
    }}).slack})
    daemon._start_slack()
    return daemon, log


async def test_new_slack_messages_reach_the_window_and_the_brain(tmp_path, monkeypatch):
    provider = FakeProvider(replies=["#4: Alex needs the brand field checked before the demo."])
    daemon, log = _slack_daemon(tmp_path, monkeypatch, provider, instructions="Never run slack.sh post.")

    _append(log, ROUND)
    daemon.clock.now += 15
    await daemon.tick()
    await daemon._commands._task

    slack = [e for e in _events(daemon) if e["type"] == "SLACK"][-1]
    assert slack["text"] == "2 new: alex.kim (DM:D0TEST), sam (#proj-search)"
    prompt = provider.prompts[-1][1]
    assert "can you check the brand field" in prompt and "it is now your turn" not in prompt
    assert prompt.endswith("Never run slack.sh post.")
    assert item_from_event(slack).text.startswith("Slack: 2 new")
    assert "alex.kim in DM:D0TEST ts=1794500000.000100: can you check" in format_event(slack)


async def test_a_round_of_bots_only_stays_silent(tmp_path, monkeypatch):
    provider = FakeProvider()
    daemon, log = _slack_daemon(tmp_path, monkeypatch, provider, ignore_authors=["alex.kim", "sam"])

    _append(log, ROUND)
    daemon.clock.now += 15
    await daemon.tick()

    assert not [e for e in _events(daemon) if e["type"] == "SLACK"] and daemon._commands is None


async def test_a_watching_session_gets_the_slack_event_instead_of_the_built_in_brain(tmp_path, monkeypatch):
    provider = FakeProvider()
    daemon, log = _slack_daemon(tmp_path, monkeypatch, provider)
    _write_json(daemon.runtime_dir / "brain.json",
                {"session_id": "abc", "pid": os.getpid(), "started": _process_started(os.getpid())})

    _append(log, ROUND)
    daemon.clock.now += 15
    await daemon.tick()

    assert [e for e in _events(daemon) if e["type"] == "SLACK"] and provider.prompts == []


def test_slack_stays_off_unless_configured_and_in_the_real_listener(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch, FakeProvider(), answer_commands=False)
    daemon.settings = daemon.settings.model_copy(update={"slack": Settings.model_validate(
        {"slack": {"enabled": True, "log": str(tmp_path / "{date}.log")}}).slack})
    daemon._start_slack()

    assert daemon._slack is None
    assert Settings().slack.enabled is False
