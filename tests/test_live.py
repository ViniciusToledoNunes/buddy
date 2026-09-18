import json
import os
from datetime import datetime, timezone

import psutil
import pytest
from typer.testing import CliRunner

from meeting_agent import cli
from meeting_agent.config import Settings
from meeting_agent.events import TranscriptEvent
from meeting_agent.listener import EventLog, ListenDaemon, format_event, submit
from meeting_agent.live import (
    LiveItem,
    LiveView,
    SessionMirror,
    brain_state,
    describe_tool,
    find_session_file,
    item_from_event,
    item_from_feed,
    numbered_items,
    register_brain,
    terminal_line,
    unregister_brain,
)
from meeting_agent.window import PAGE, Pump, WindowApi


class FakeStream:
    def __init__(self, speaker):
        self.speaker = speaker

    def start(self):
        pass

    async def stop(self):
        pass


def _daemon(tmp_path):
    settings = Settings.model_validate({"meetings_dir": str(tmp_path / "meetings"), "listen": {"sounds": False}})
    daemon = ListenDaemon(settings, tmp_path / "runtime", stream_factory=FakeStream)
    daemon.chime = lambda kind: None
    daemon.claim()
    return daemon


def _lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _shown(daemon):
    return [(r["type"], r.get("speaker", ""), r.get("text", "")) for r in _lines(daemon.runtime_dir / "live.jsonl")]


def _events(daemon):
    return _lines(daemon.runtime_dir / "events.jsonl")


def _said(speaker, text):
    return TranscriptEvent(speaker, text, True, "u")


def _alive(runtime):
    started = datetime.fromtimestamp(psutil.Process(os.getpid()).create_time(), timezone.utc).isoformat()
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "listen.json").write_text(
        json.dumps({"pid": os.getpid(), "started": started, "state": "listening"}), encoding="utf-8"
    )


# ------------------------------------------------------------ what is shown


async def test_meeting_speech_is_shown_as_it_is_said(tmp_path):
    daemon = _daemon(tmp_path)
    await daemon.start_meeting("test")

    await daemon.on_final(_said("REMOTE", "Can everyone see my screen?"))
    await daemon.on_final(_said("ME", "Yes, go ahead."))

    assert _shown(daemon) == [("SAID", "REMOTE", "Can everyone see my screen?"), ("SAID", "ME", "Yes, go ahead.")]


async def test_speech_outside_a_meeting_is_never_shown(tmp_path):
    """The listener hears everything to find the wake phrase; the screen must not."""
    daemon = _daemon(tmp_path)

    await daemon.on_final(_said("ME", "I think I'll grab lunch now."))

    assert _shown(daemon) == []


async def test_a_dictated_command_is_shown_piece_by_piece(tmp_path):
    daemon = _daemon(tmp_path)

    await daemon.on_final(_said("ME", "Hey Buddy, review PR 123"))
    await daemon.on_final(_said("ME", "and check the tests. Over and out."))

    assert [text for _, _, text in _shown(daemon)] == [
        "Hey Buddy, review PR 123",
        "and check the tests. Over and out.",
    ]
    assert _events(daemon)[-1]["text"] == "review PR 123 and check the tests"


async def test_a_cancelled_command_says_so_on_screen(tmp_path):
    daemon = _daemon(tmp_path)

    await daemon.on_final(_said("ME", "Hey Buddy, post that"))
    await daemon.on_final(_said("ME", "Never mind."))

    assert _shown(daemon)[-1][0] == "CANCELLED"


async def test_a_full_display_feed_starts_over_instead_of_piling_up(tmp_path):
    log = EventLog(tmp_path / "live.jsonl", max_bytes=200, archive=False)
    for n in range(20):
        log.append("SAID", text=f"line {n} " + "x" * 40)

    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("live")] == ["live.jsonl", "live.meta.json"]


# ---------------------------------------------------------------- typed


async def test_a_typed_command_reaches_the_session_whole(tmp_path):
    daemon = _daemon(tmp_path)

    submit(daemon.runtime_dir, "review PR 123")
    await daemon.check_controls()

    command = _events(daemon)[-1]
    assert (command["type"], command["text"], command["ended"]) == ("COMMAND", "review PR 123", "typed")
    assert format_event(command).endswith('typed="review PR 123"')
    assert "incomplete" not in format_event(command)


async def test_typed_commands_arrive_in_order_and_work_while_paused(tmp_path):
    daemon = _daemon(tmp_path)
    await daemon.pause("test")

    submit(daemon.runtime_dir, "first")
    submit(daemon.runtime_dir, "second")
    await daemon.check_controls()

    assert [e["text"] for e in _events(daemon) if e["type"] == "COMMAND"] == ["first", "second"]


async def test_a_typed_control_command_is_handled_by_buddy(tmp_path):
    daemon = _daemon(tmp_path)

    submit(daemon.runtime_dir, "the meeting is starting")
    await daemon.check_controls()

    assert daemon.state == "meeting"
    assert not [e for e in _events(daemon) if e["type"] == "COMMAND"]


async def test_a_typed_stop_turns_buddy_off(tmp_path):
    daemon = _daemon(tmp_path)

    submit(daemon.runtime_dir, "stop")

    assert await daemon.check_controls() is False
    assert daemon._shutdown_reason == "typed"


def test_a_command_left_by_a_dead_listener_is_not_replayed(tmp_path):
    runtime = tmp_path / "runtime"
    submit(runtime, "merge it")
    settings = Settings.model_validate({"meetings_dir": str(tmp_path / "meetings")})

    ListenDaemon(settings, runtime, stream_factory=FakeStream).claim()

    assert not list((runtime / "listen").glob("command-*.json"))


# ------------------------------------------------------- event to screen


def test_feed_and_event_records_become_lines():
    said = item_from_feed({"type": "SAID", "speaker": "REMOTE", "text": "Hi", "time": "2026-09-18T15:00:00+00:00"})
    silence = item_from_event({"type": "COMMAND", "text": "check it", "ended": "silence", "time": "x"})

    assert (said.kind, said.who, said.text) == ("said", "REMOTE", "Hi")
    assert said.clock
    assert (silence.who, silence.note) == ("voice", "sent after a silence; may be incomplete")
    assert item_from_event({"type": "MEETING_BATCH", "lines": ["ME: hi"]}) is None  # already shown live
    assert item_from_event({"type": "MEETING_END", "minutes": 12}).text == "Meeting ended after 12 min."
    assert item_from_event({"type": "NOTICE", "text": "Already recording."}).kind == "notice"
    assert item_from_feed({"type": "UNKNOWN"}) is None


def test_terminal_lines_never_interpret_what_was_said():
    line = terminal_line(LiveItem("said", "[red]not markup[/red]", who="REMOTE", at=1.0))

    assert "\\[red]" in line  # escaped, so a speaker cannot style or break the output


def test_tool_calls_read_as_what_claude_is_doing():
    assert describe_tool("Bash", {"command": "gh pr view 123", "description": "Read PR 123"}) == "Read PR 123"
    assert describe_tool("Bash", {"command": "git log -3\ngit status"}) == "$ git log -3"
    assert describe_tool("Read", {"file_path": "C:/code/src/retry.py"}) == "Read retry.py"
    assert describe_tool("Grep", {"pattern": "retry_policy"}) == "Grep retry_policy"
    assert describe_tool("WebFetch", {"url": "https://example.com"}) == "Fetch https://example.com"
    assert describe_tool("mcp__buddy__get_meeting", {}) == "buddy: get_meeting"
    assert describe_tool("Monitor", {"description": "Buddy listener"}) == "Monitor: Buddy listener"
    assert describe_tool("Something", None) == "Something"


def test_numbered_suggestions_are_picked_out_of_a_reply():
    reply = "Worth saying now:\n\n4. Ask who owns the rollback plan\n**5.** Mention PR 123 is blocked\nThat's it."

    assert numbered_items(reply) == ["4. Ask who owns the rollback plan", "5. Mention PR 123 is blocked"]


# ------------------------------------------------------ the session mirror


BUDDY_BATCH = (
    '<task-notification>\n<summary>Monitor event: "Buddy listener"</summary>\n'
    "<event>15:00:00 MEETING_BATCH meeting=m lines=1 | REMOTE: who owns rollback?</event>\n</task-notification>"
)
BUDDY_COMMAND = (
    '<task-notification>\n<summary>Monitor event: "Buddy listener"</summary>\n'
    '<event>15:00:00 COMMAND typed="review PR 123"</event>\n</task-notification>'
)
OTHER_MONITOR = '<task-notification>\n<summary>Monitor event: "Slack"</summary>\n<event>new message</event>\n</task-notification>'


def _session(tmp_path):
    path = tmp_path / "projects" / "c--work" / "abc12345-0000.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "user", "message": {"content": "from before the window"}}) + "\n")
    return path


def _write(path, *records):
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps({"timestamp": "2026-09-18T15:00:01.000Z", **record}) + "\n")


def _queued(prompt):
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": prompt}}


def _assistant(*blocks, **extra):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}, **extra}


def _user(content):
    return {"type": "user", "message": {"role": "user", "content": content}}


def test_the_mirror_shows_what_claude_does_with_a_buddy_event(tmp_path):
    path = _session(tmp_path)
    mirror = SessionMirror(path)

    _write(
        path,
        _queued(BUDDY_COMMAND),
        _assistant({"type": "thinking", "thinking": "..."}),
        _assistant({"type": "tool_use", "name": "Bash", "input": {"command": "gh pr view 123", "description": "Read PR 123"}}),
        _user([{"type": "tool_result", "content": "diff..."}]),
        _assistant({"type": "text", "text": "PR 123 changes the retry policy."}),
    )

    assert [(i.kind, i.text) for i in mirror.poll()] == [
        ("tool", "Read PR 123"),
        ("claude", "PR 123 changes the retry policy."),
    ]


def test_the_rest_of_the_session_stays_private_unless_asked(tmp_path):
    path = _session(tmp_path)
    private, everything = SessionMirror(path), SessionMirror(path, show="all")

    _write(
        path,
        _user("what should I cook tonight?"),
        _assistant({"type": "text", "text": "Risotto."}),
        _queued(OTHER_MONITOR),
        _assistant({"type": "text", "text": "Nothing new on Slack."}),
    )

    assert private.poll() == []
    assert [(i.kind, i.text) for i in everything.poll()] == [
        ("you", "what should I cook tonight?"),
        ("claude", "Risotto."),
        ("claude", "Nothing new on Slack."),
    ]


def test_suggestions_come_from_the_reply_to_meeting_speech(tmp_path):
    path = _session(tmp_path)
    mirror = SessionMirror(path)

    _write(path, _queued(BUDDY_BATCH), _assistant({"type": "text", "text": "1. Ask who owns rollback\n2. Offer to take it"}))
    mirror.poll()
    assert mirror.suggestions == ["1. Ask who owns rollback", "2. Offer to take it"]

    # A numbered list in the answer to a command is not a meeting suggestion.
    _write(path, _queued(BUDDY_COMMAND), _assistant({"type": "text", "text": "1. Fetch the PR\n2. Read the tests"}))
    mirror.poll()
    assert mirror.suggestions == ["1. Ask who owns rollback", "2. Offer to take it"]


def test_the_mirror_skips_subagents_system_notes_and_half_written_lines(tmp_path):
    path = _session(tmp_path)
    mirror = SessionMirror(path)

    _write(
        path,
        _queued(BUDDY_COMMAND),
        _assistant({"type": "text", "text": "subagent chatter"}, isSidechain=True),
        {"type": "user", "isMeta": True, "message": {"content": "skill text"}},
        _user("<command-name>/clear</command-name>"),
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "assistant", "message": {"content": [{"type": "text", "text": "hal')

    assert mirror.poll() == []
    with path.open("a", encoding="utf-8") as handle:
        handle.write('f done"}]}}\n')
    assert [i.text for i in mirror.poll()] == ["half done"]


def test_the_mirror_survives_an_unreadable_or_missing_record(tmp_path):
    path = _session(tmp_path)
    mirror = SessionMirror(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n[1, 2]\n")

    assert mirror.poll() == []
    path.unlink()
    assert mirror.poll() == []


# ------------------------------------------------------------- the brain


def test_the_watching_session_is_found_by_its_id(tmp_path, monkeypatch):
    path = _session(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    assert find_session_file("abc12345-0000") == path
    assert find_session_file("missing-0000") is None
    assert find_session_file("../../etc") is None  # it goes into a glob


def test_the_brain_record_belongs_to_the_watch_that_wrote_it(tmp_path):
    runtime = tmp_path / "runtime"
    register_brain(runtime, "abc12345-0000", "claude-code")

    assert brain_state(runtime)["watching"] is True
    unregister_brain(runtime)
    assert brain_state(runtime) is None

    (runtime / "brain.json").write_text(json.dumps({"session_id": "abc12345-0000", "pid": 1, "started": "x"}))
    unregister_brain(runtime)  # a newer watch's record is left alone
    assert brain_state(runtime)["watching"] is False


# -------------------------------------------------------------- the view


async def test_the_view_merges_the_listener_and_the_session(tmp_path):
    daemon = _daemon(tmp_path)
    runtime = daemon.runtime_dir
    session = _session(tmp_path)
    view = LiveView(runtime, home=tmp_path)
    register_brain(runtime, "abc12345-0000", "claude-code")

    await daemon.start_meeting("test")
    await daemon.on_final(_said("REMOTE", "Who owns the rollback?"))
    first = view.poll()
    _write(session, _queued(BUDDY_BATCH), _assistant({"type": "text", "text": "1. Say you will own it"}))
    second = view.poll()

    assert [(i.kind, i.text) for i in first] == [
        ("state", "Meeting started: recording."),
        ("said", "Who owns the rollback?"),
    ]
    assert [i.kind for i in second] == ["claude"]
    status = view.status()
    assert (status["listener"], status["brain"], status["suggestions"]) == ("meeting", "watching", ["1. Say you will own it"])

    await daemon.stop_meeting("test")
    await daemon.start_meeting("test")
    view.poll()
    assert view.status()["suggestions"] == []  # a new meeting starts clean


def test_the_view_reports_buddy_off_and_no_session(tmp_path):
    view = LiveView(tmp_path / "runtime", home=tmp_path)

    assert view.poll() == []
    assert (view.status()["listener"], view.status()["brain"]) == ("off", "none")
    view.set_show("all")
    assert view.status()["show"] == "all"


# ------------------------------------------------------------ the window


def test_the_window_sends_typed_commands_and_controls(tmp_path):
    runtime = tmp_path / "runtime"
    sent, controlled = [], []
    api = WindowApi(runtime, LiveView(runtime), lambda r, t: sent.append(t), lambda r, a: controlled.append(a))

    assert api.send("review PR 123")["ok"] is False  # Buddy is off
    _alive(runtime)
    assert api.send("  review PR 123  ") == {"ok": True}
    assert api.send("   ")["ok"] is False
    assert api.send("x" * 5000)["ok"] is False
    assert api.control("start-meeting") == {"ok": True}
    assert api.control("rm -rf")["ok"] is False

    assert sent == ["review PR 123"]
    assert controlled == ["start-meeting"]


def test_the_window_filters_and_pins(tmp_path):
    runtime = tmp_path / "runtime"
    view = LiveView(runtime)
    api = WindowApi(runtime, view)

    assert api.set_show("all") == {"ok": True, "show": "all"}
    assert api.set_show("everything")["show"] == "all"

    class Window:
        on_top = True

    api._window = Window()
    api.set_on_top(False)
    assert api._window.on_top is False


async def test_the_pump_pushes_only_what_changed(tmp_path):
    daemon = _daemon(tmp_path)
    pushed = []
    pump = Pump(LiveView(daemon.runtime_dir), pushed.append, status_every=100)

    assert pump.step() is True  # the first status
    assert pump.step() is False
    await daemon.start_meeting("test")
    assert pump.step() is True

    payload = json.loads(pushed[-1][len("window.buddy && window.buddy.update("):-1])
    assert payload["items"][0]["text"] == "Meeting started: recording."
    assert payload["status"]["listener"] == "meeting"


def test_the_page_never_renders_text_as_html():
    """Transcripts and replies are untrusted: a speaker must not be able to inject markup."""
    assert "innerHTML" not in PAGE
    assert "insertAdjacentHTML" not in PAGE
    assert "textContent" in PAGE


# ----------------------------------------------------------------- the CLI


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    path = tmp_path / "runtime"
    monkeypatch.setattr(cli, "_runtime_dir", lambda: path)
    return path


def test_say_needs_a_listener_and_then_hands_the_command_over(runtime):
    runner = CliRunner()

    assert runner.invoke(cli.app, ["say", "review", "PR", "123"]).exit_code == 1
    _alive(runtime)
    assert runner.invoke(cli.app, ["say", "review", "PR", "123"]).exit_code == 0

    [submitted] = (runtime / "listen").glob("command-*.json")
    assert json.loads(submitted.read_text(encoding="utf-8"))["text"] == "review PR 123"


def test_a_watch_run_by_a_session_registers_it_as_the_brain(runtime, monkeypatch):
    import meeting_agent.live as live

    registered = []
    monkeypatch.setattr(live, "register_brain", lambda r, s, c: registered.append((s, c)))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc12345-0000")

    CliRunner().invoke(cli.app, ["watch", "--follow", "--as", "claude-code"])
    CliRunner().invoke(cli.app, ["watch", "--follow", "--peek", "--as", "claude-code"])

    assert registered == [("abc12345-0000", "claude-code")]  # a peek is not the brain
