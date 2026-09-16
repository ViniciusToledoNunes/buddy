import asyncio
import json
import os
from datetime import datetime, timezone

import psutil
import pytest
from typer.testing import CliRunner

from meeting_agent import cli
from meeting_agent.config import Settings
from meeting_agent.context import MeetingRepository
from meeting_agent.events import TranscriptEvent
from meeting_agent.listener import (
    SUMMARY_PENDING,
    EventLog,
    EventReader,
    ListenDaemon,
    MeetingBatcher,
    classify_command,
    format_event,
    listener_state,
    parse_wake,
    request,
)


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("Hey Buddy, the meeting is starting.", (True, "the meeting is starting.")),
        ("Hey, buddy. Review PR 123.", (True, "Review PR 123.")),
        ("  hey buddy!", (True, "")),
        ("OK Buddy, pause listening", (True, "pause listening")),
        ("Hey Bodie, the call is over", (True, "the call is over")),
        ("hey everybody, welcome", (False, "")),
        ("A buddy of mine said hi", (False, "")),
        ("Thanks buddy, that helps", (False, "")),
        ("", (False, "")),
    ],
)
def test_the_wake_phrase_must_open_the_utterance(said, expected):
    """"buddy" alone is ordinary English; only "hey buddy" at the start counts."""
    assert parse_wake(said) == expected


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("the meeting is starting", "start_meeting"),
        ("start recording", "start_meeting"),
        ("a call is about to begin", "start_meeting"),
        ("the meeting is over", "stop_meeting"),
        ("stop the recording", "stop_meeting"),
        ("the call ended", "stop_meeting"),
        ("pause listening", "pause"),
        ("stop listening for a while", "pause"),
        ("review PR 123", "other"),
        ("post a comment on Slack", "other"),
        # Both directions at once: Claude sorts it out instead of Buddy guessing.
        ("end the meeting and start the summary", "other"),
    ],
)
def test_commands_buddy_settles_itself(command, kind):
    assert classify_command(command) == kind


# ---------------------------------------------------------------- event log


def test_sequence_numbers_survive_a_restart(tmp_path):
    first = EventLog(tmp_path / "events.jsonl").append("LISTENER_UP")
    second = EventLog(tmp_path / "events.jsonl").append("COMMAND", text="hi")

    assert (first["seq"], second["seq"]) == (1, 2)


def test_a_new_watcher_starts_at_the_end(tmp_path):
    """Replaying every past meeting to a freshly armed monitor would bury the live one."""
    log = EventLog(tmp_path / "events.jsonl")
    log.append("MEETING_END", meeting="old")
    reader = EventReader(log.path, tmp_path / "cursors" / "new.json")
    cursor = reader.load_cursor()

    log.append("COMMAND", text="fresh")
    records, _ = reader.read(cursor)

    assert [r["type"] for r in records] == ["COMMAND"]


def test_named_watchers_do_not_consume_each_other(tmp_path):
    """The Slack watcher's shared state meant whoever ran first swallowed the news."""
    log = EventLog(tmp_path / "events.jsonl")
    a = EventReader(log.path, tmp_path / "cursors" / "a.json")
    b = EventReader(log.path, tmp_path / "cursors" / "b.json")
    ca, cb = a.load_cursor(), b.load_cursor()
    log.append("COMMAND", text="one")

    got_a, ca = a.read(ca)
    a.save_cursor(ca)
    got_b, _ = b.read(cb)

    assert len(got_a) == len(got_b) == 1


def test_a_saved_cursor_resumes_where_it_stopped(tmp_path):
    """A monitor that expired catches up on re-arm instead of losing what was said."""
    log = EventLog(tmp_path / "events.jsonl")
    reader = EventReader(log.path, tmp_path / "cursors" / "s.json")
    reader.save_cursor(reader.load_cursor())
    log.append("COMMAND", text="while the monitor was down")

    records, _ = EventReader(log.path, tmp_path / "cursors" / "s.json").read(reader.load_cursor())

    assert records[0]["text"] == "while the monitor was down"


def test_a_line_still_being_written_is_left_for_later(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    reader = EventReader(log.path, tmp_path / "c.json")
    cursor = reader.load_cursor()
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 1, "type": "COMM')

    records, after = reader.read(cursor)

    assert records == [] and after["offset"] == cursor["offset"]


def test_rotation_restarts_the_offset_without_repeating_events(tmp_path):
    log = EventLog(tmp_path / "events.jsonl", max_bytes=200)
    reader = EventReader(log.path, tmp_path / "c.json")
    cursor = reader.load_cursor()
    for index in range(4):
        log.append("COMMAND", text=f"padding {index} " + "x" * 60)
    seen, cursor = reader.read(cursor)
    log.append("COMMAND", text="after rotation")

    records, _ = reader.read(cursor)

    assert len(list(tmp_path.glob("events.*.jsonl"))) >= 1
    assert [r["text"] for r in records] == ["after rotation"]
    assert not {r["seq"] for r in records} & {r["seq"] for r in seen}


# ---------------------------------------------------------------- rendering


def test_every_event_renders_on_one_line():
    records = [
        {"type": "COMMAND", "time": "2026-09-16T18:00:00+00:00", "text": "review PR 123", "meeting": ""},
        {"type": "MEETING_START", "time": "2026-09-16T18:00:00+00:00", "meeting": "m1", "reason": "voice", "transcript": "t.txt"},
        {"type": "MEETING_END", "time": "2026-09-16T18:00:00+00:00", "meeting": "m1", "minutes": 3.5, "lines": 40, "transcript": "t.txt"},
        {"type": "NOTICE", "time": "2026-09-16T18:00:00+00:00", "text": "Already recording."},
        {"type": "MEETING_BATCH", "time": "bad", "meeting": "m1", "lines": ["ME: a\nb", "REMOTE: c"]},
    ]

    rendered = [format_event(r) for r in records]

    assert 'COMMAND said="review PR 123"' in rendered[0]
    assert "reason=voice" in rendered[1] and "transcript=t.txt" in rendered[1]
    assert "minutes=3.5" in rendered[2] and "lines=40" in rendered[2]
    assert rendered[3].endswith("Already recording.")
    assert "lines=2" in rendered[4] and "REMOTE: c" in rendered[4]


def test_a_long_batch_is_truncated_with_a_pointer():
    record = {"type": "MEETING_BATCH", "time": "2026-09-16T18:00:00+00:00", "lines": ["REMOTE: " + "y" * 900] * 20,
              "transcript": "C:/m/transcript.txt"}

    text = format_event(record, max_chars=1000)

    assert len(text) <= 1000
    assert text.endswith("(full text in C:/m/transcript.txt)")


def test_batches_wait_for_a_pause_and_the_interval():
    """One notification per line would get the monitor stopped as a firehose."""
    batcher = MeetingBatcher(min_interval=60, pause=3, max_wait=150)
    batcher.last_emit = 0
    batcher.add("REMOTE: one", now=10)

    assert not batcher.due(12)  # still talking
    assert not batcher.due(40)  # paused, but the interval has not passed
    assert batcher.due(61)
    assert batcher.take(61) == ["REMOTE: one"]

    batcher.add("REMOTE: nonstop", now=70)
    for moment in range(71, 220):
        batcher.add("REMOTE: nonstop", now=moment)
    assert batcher.due(220)  # a monologue still flushes after max_wait


# ------------------------------------------------------------------- daemon


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


class FakeStream:
    def __init__(self, speaker, log):
        self.speaker = speaker
        self.log = log

    def start(self):
        self.log.append(("start", self.speaker))

    async def stop(self):
        self.log.append(("stop", self.speaker))


def _daemon(tmp_path, clock=None, **listen):
    settings = Settings.model_validate({"meetings_dir": str(tmp_path / "meetings"), "listen": listen})
    streams = []
    daemon = ListenDaemon(
        settings, tmp_path / "runtime", stream_factory=lambda s: FakeStream(s, streams), clock=clock or Clock()
    )
    daemon.claim()
    return daemon, streams


def _events(daemon):
    path = daemon.runtime_dir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _said(speaker, text):
    return TranscriptEvent(speaker, text, True, "u")


async def test_speech_outside_a_meeting_is_heard_not_kept(tmp_path):
    daemon, _ = _daemon(tmp_path)
    await daemon.start_listening()

    await daemon.on_final(_said("ME", "I think we should order lunch"))

    assert _events(daemon) == []
    assert not (tmp_path / "meetings").exists()


async def test_a_voice_command_opens_a_meeting_that_mcp_sees(tmp_path):
    daemon, streams = _daemon(tmp_path)
    await daemon.start_listening()

    await daemon.on_final(_said("ME", "Hey Buddy, the meeting is starting."))

    assert ("start", "REMOTE") in streams
    start = _events(daemon)[-1]
    assert start["type"] == "MEETING_START" and start["reason"] == "voice"
    # The MCP status check compares the recorded start with the process start time.
    status = MeetingRepository(tmp_path / "meetings", daemon.runtime_dir).status()
    assert status["active"] is True
    assert status["meeting_dir"].endswith(start["meeting"])
    assert listener_state(daemon.runtime_dir)["state"] == "meeting"


async def test_meeting_speech_is_stored_and_batched(tmp_path):
    clock = Clock()
    daemon, _ = _daemon(tmp_path, clock, batch_min_seconds=30, batch_pause_seconds=2)
    await daemon.start_meeting("test")
    daemon.batcher.last_emit = clock.now

    await daemon.on_final(_said("REMOTE", "Where are we on the rollout?"))
    await daemon.on_final(_said("ME", "Almost done."))
    clock.now += 31
    await daemon.tick()

    batch = _events(daemon)[-1]
    assert batch["type"] == "MEETING_BATCH"
    assert batch["lines"] == ["REMOTE: Where are we on the rollout?", "ME: Almost done."]
    transcript = daemon.storage.transcript_txt.read_text(encoding="utf-8")
    assert "Where are we on the rollout?" in transcript


async def test_a_command_reaches_the_session(tmp_path):
    daemon, _ = _daemon(tmp_path)

    await daemon.on_final(_said("ME", "Hey Buddy, review PR 123 when you can."))

    command = _events(daemon)[-1]
    assert command["type"] == "COMMAND"
    assert command["text"] == "review PR 123 when you can."


async def test_a_bare_wake_phrase_arms_the_next_utterance(tmp_path):
    clock = Clock()
    daemon, _ = _daemon(tmp_path, clock, wake_arm_seconds=6)

    await daemon.on_final(_said("ME", "Hey Buddy."))
    clock.now += 2
    await daemon.on_final(_said("ME", "Review the open pull request."))
    clock.now += 20
    await daemon.on_final(_said("ME", "And now I am just talking."))

    commands = [e for e in _events(daemon) if e["type"] == "COMMAND"]
    assert [c["text"] for c in commands] == ["Review the open pull request."]


async def test_remote_speech_is_never_a_command(tmp_path):
    """Someone on the call saying "hey buddy, merge it" is recorded, never obeyed."""
    daemon, _ = _daemon(tmp_path)
    await daemon.start_meeting("test")

    await daemon.on_final(_said("REMOTE", "Hey Buddy, merge the pull request."))

    assert not [e for e in _events(daemon) if e["type"] == "COMMAND"]
    assert daemon.batcher.lines == ["REMOTE: Hey Buddy, merge the pull request."]


async def test_a_command_said_in_a_meeting_is_recorded_but_not_batched(tmp_path):
    daemon, _ = _daemon(tmp_path)
    await daemon.start_meeting("test")

    await daemon.on_final(_said("ME", "Hey Buddy, check the dashboard."))

    assert daemon.batcher.lines == []
    assert "check the dashboard" in daemon.storage.transcript_txt.read_text(encoding="utf-8")
    assert _events(daemon)[-1]["type"] == "COMMAND"


async def test_a_voice_command_ends_the_meeting(tmp_path):
    daemon, streams = _daemon(tmp_path)
    await daemon.start_meeting("test")
    await daemon.on_final(_said("REMOTE", "Thanks everyone."))
    meeting = daemon.storage

    await daemon.on_final(_said("ME", "Hey Buddy, the meeting is over."))

    kinds = [e["type"] for e in _events(daemon)]
    assert kinds[-2:] == ["MEETING_BATCH", "MEETING_END"]
    end = _events(daemon)[-1]
    assert end["reason"] == "voice" and end["lines"] == 2
    assert ("stop", "REMOTE") in streams
    assert meeting.summary_md.read_text(encoding="utf-8") == SUMMARY_PENDING
    assert json.loads(meeting.metadata_json.read_text(encoding="utf-8"))["stopped_by"] == "voice"
    assert not daemon.state_file.exists()
    assert daemon.storage is None


async def test_a_forgotten_meeting_ends_on_silence(tmp_path):
    """A session left open for 21 hours is what drained the API credit in August."""
    clock = Clock()
    daemon, _ = _daemon(tmp_path, clock, meeting_idle_minutes=10)
    await daemon.start_meeting("test")

    clock.now += 9 * 60
    await daemon.tick()
    assert daemon.storage is not None
    clock.now += 2 * 60
    await daemon.tick()

    assert _events(daemon)[-1]["reason"] == "idle"


async def test_a_meeting_has_a_ceiling(tmp_path):
    clock = Clock()
    daemon, _ = _daemon(tmp_path, clock, meeting_idle_minutes=10, meeting_max_minutes=30)
    await daemon.start_meeting("test")
    for _ in range(40):
        clock.now += 60
        await daemon.on_final(_said("REMOTE", "a video that never stops"))
        await daemon.tick()

    assert [e["reason"] for e in _events(daemon) if e["type"] == "MEETING_END"] == ["max_duration"]


async def test_double_starts_and_stray_stops_explain_themselves(tmp_path):
    """The session is the user's only feedback channel, so a no-op still says so."""
    daemon, _ = _daemon(tmp_path)

    await daemon.stop_meeting("voice")
    await daemon.start_meeting("voice")
    await daemon.start_meeting("voice")

    notices = [e["text"] for e in _events(daemon) if e["type"] == "NOTICE"]
    assert notices == ["No meeting is being recorded.", "Already recording this meeting."]


async def test_pausing_closes_the_microphone_and_ends_the_meeting(tmp_path):
    daemon, streams = _daemon(tmp_path)
    await daemon.start_listening()
    await daemon.start_meeting("test")

    await daemon.on_final(_said("ME", "Hey Buddy, stop listening."))

    assert daemon.paused and daemon.mic is None
    assert ("stop", "ME") in streams and ("stop", "REMOTE") in streams
    assert [e["type"] for e in _events(daemon)][-2:] == ["MEETING_END", "PAUSED"]

    await daemon.resume("requested")
    assert not daemon.paused and streams[-1] == ("start", "ME")


async def test_the_cli_and_mcp_can_steer_the_listener(tmp_path):
    clock = Clock()
    daemon, _ = _daemon(tmp_path, clock)
    runtime = daemon.runtime_dir

    request(runtime, "start-meeting")
    assert await daemon.check_controls()
    assert daemon.storage is not None

    await daemon.on_final(_said("REMOTE", "status please"))
    daemon.suggest_file.touch()  # MCP request_suggestion flushes what is pending
    await daemon.check_controls()
    assert _events(daemon)[-1]["type"] == "MEETING_BATCH"

    daemon.stop_file.touch()  # `buddy stop` and MCP stop_meeting
    await daemon.check_controls()
    assert daemon.storage is None

    request(runtime, "pause")
    await daemon.check_controls()
    assert daemon.paused
    request(runtime, "resume")
    await daemon.check_controls()
    assert not daemon.paused

    request(runtime, "stop-meeting")
    await daemon.check_controls()
    assert _events(daemon)[-1]["type"] == "NOTICE"

    request(runtime, "shutdown")
    assert await daemon.check_controls() is False


def _alive_state():
    started = datetime.fromtimestamp(psutil.Process(os.getpid()).create_time(), timezone.utc).isoformat()
    return {"pid": os.getpid(), "started": started}


def test_only_one_thing_may_hold_the_microphone(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    settings = Settings.model_validate({"meetings_dir": str(tmp_path / "meetings")})

    (runtime / "listen.json").write_text(json.dumps(_alive_state()), encoding="utf-8")
    with pytest.raises(RuntimeError, match="already listening"):
        ListenDaemon(settings, runtime).claim()

    (runtime / "listen.json").unlink()
    (runtime / "state.json").write_text(json.dumps(_alive_state()), encoding="utf-8")
    with pytest.raises(RuntimeError, match="session is active"):
        ListenDaemon(settings, runtime).claim()


def test_stale_state_from_a_crash_does_not_block_a_restart(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "listen").mkdir(parents=True)
    dead = {"pid": 999_999_999, "started": "2026-01-01T00:00:00+00:00"}
    (runtime / "listen.json").write_text(json.dumps(dead), encoding="utf-8")
    (runtime / "state.json").write_text(json.dumps(dead), encoding="utf-8")
    (runtime / "listen" / "stop-meeting.flag").touch()
    settings = Settings.model_validate({"meetings_dir": str(tmp_path / "meetings")})

    ListenDaemon(settings, runtime, stream_factory=lambda s: FakeStream(s, [])).claim()

    assert not (runtime / "state.json").exists()
    assert not (runtime / "listen" / "stop-meeting.flag").exists()


async def test_the_loop_runs_and_cleans_up(tmp_path):
    runtime = tmp_path / "runtime"
    settings = Settings.model_validate({"meetings_dir": str(tmp_path / "meetings")})
    streams = []
    daemon = ListenDaemon(settings, runtime, stream_factory=lambda s: FakeStream(s, streams))
    stop = asyncio.Event()

    task = asyncio.create_task(daemon.run(stop))
    await asyncio.sleep(0.2)
    daemon.bus.publish(_said("ME", "Hey Buddy, start the meeting."))
    await asyncio.sleep(0.3)
    daemon.bus.publish(_said("REMOTE", "hello"))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, 5)

    kinds = [e["type"] for e in _events(daemon)]
    assert kinds[0] == "LISTENER_UP" and kinds[-1] == "LISTENER_DOWN"
    assert "MEETING_START" in kinds and "MEETING_END" in kinds
    assert not (runtime / "listen.json").exists()
    assert ("stop", "ME") in streams


async def test_a_shutdown_request_ends_the_loop(tmp_path):
    runtime = tmp_path / "runtime"
    settings = Settings.model_validate({"meetings_dir": str(tmp_path / "meetings")})
    daemon = ListenDaemon(settings, runtime, stream_factory=lambda s: FakeStream(s, []))

    task = asyncio.create_task(daemon.run(asyncio.Event()))
    await asyncio.sleep(0.2)
    request(runtime, "shutdown")
    await asyncio.wait_for(task, 5)

    assert _events(daemon)[-1]["reason"] == "shutdown requested"


# ---------------------------------------------------------------------- cli


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    path = tmp_path / "runtime"
    monkeypatch.setattr(cli, "_runtime_dir", lambda: path)
    return path


def test_watch_prints_and_advances_its_own_cursor(runtime):
    runner = CliRunner()
    runner.invoke(cli.app, ["watch", "--as", "t"])  # creates the cursor at the end
    EventLog(runtime / "events.jsonl").append("COMMAND", text="review PR 123")

    peeked = runner.invoke(cli.app, ["watch", "--as", "t", "--peek"])
    first = runner.invoke(cli.app, ["watch", "--as", "t"])
    second = runner.invoke(cli.app, ["watch", "--as", "t"])
    other = runner.invoke(cli.app, ["watch", "--as", "someone-else"])

    assert 'said="review PR 123"' in peeked.stdout
    assert 'said="review PR 123"' in first.stdout
    assert "review PR 123" not in second.stdout
    assert "review PR 123" not in other.stdout  # a new reader starts at the end
    assert "LISTENER_DOWN" in second.stdout  # nobody is listening in this test


def test_watch_can_emit_raw_json(runtime):
    runner = CliRunner()
    runner.invoke(cli.app, ["watch", "--as", "j"])
    EventLog(runtime / "events.jsonl").append("COMMAND", text="hi")

    result = runner.invoke(cli.app, ["watch", "--as", "j", "--json"])

    assert json.loads(result.stdout.splitlines()[0])["text"] == "hi"


@pytest.mark.parametrize("arguments", [["meeting", "start"], ["pause"], ["resume"], ["listen", "--stop"]])
def test_steering_needs_a_running_listener(runtime, arguments):
    result = CliRunner().invoke(cli.app, arguments)

    assert result.exit_code == 1
    assert "not listening" in result.stdout


def test_meeting_rejects_an_unknown_action(runtime):
    result = CliRunner().invoke(cli.app, ["meeting", "restart"])

    assert result.exit_code == 2


def test_steering_writes_a_request_when_listening(runtime, monkeypatch):
    monkeypatch.setattr("meeting_agent.listener.listener_state", lambda *_: {"pid": 1})

    result = CliRunner().invoke(cli.app, ["meeting", "stop"])

    assert result.exit_code == 0
    assert (runtime / "listen" / "stop-meeting.flag").exists()
