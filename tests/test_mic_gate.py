import io
import json

import pytest
from typer.testing import CliRunner

from meeting_agent import cli, mic_gate
from meeting_agent.events import TranscriptEvent
from meeting_agent.mic_gate import MicGate, read_mode, write_mode

from test_listener import Clock, _daemon, _events


class Process:
    """A stand-in for buddy-mic-state: its output lines, then the end of the stream."""

    def __init__(self, lines):
        self.stdout = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
        self.stopped = False

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


def _gate(tmp_path, lines=(), clock=None, **options):
    return MicGate(tmp_path / "mic.json", spawn=lambda: Process(lines), clock=clock or Clock(), **options)


def test_the_gate_follows_the_apps_state(tmp_path):
    clock = Clock()
    gate = _gate(tmp_path, clock=clock)

    gate.update('{"state": "muted", "app": "Microsoft Teams", "label": "Unmute mic"}')
    assert not gate.heard() and gate.app == "Microsoft Teams"
    gate.update('{"state": "open", "app": "Microsoft Teams", "label": "Mute mic"}')
    assert gate.heard()


def test_a_line_said_just_before_muting_again_is_kept(tmp_path):
    clock = Clock()
    gate = _gate(tmp_path, clock=clock, grace_seconds=15)
    gate.update('{"state": "open"}')
    clock.now += 4
    gate.update('{"state": "muted"}')

    clock.now += 10
    assert gate.heard()  # transcribed a few seconds after it was said
    clock.now += 6
    assert not gate.heard()


def test_with_no_app_read_the_setting_decides(tmp_path):
    keep = _gate(tmp_path, when_unknown="keep")
    drop = _gate(tmp_path, when_unknown="drop")

    assert keep.heard() and not drop.heard()


def test_the_manual_mode_overrides_the_app(tmp_path):
    gate = _gate(tmp_path)
    gate.update('{"state": "muted"}')

    write_mode(tmp_path / "mic.json", "on")
    assert gate.heard()
    write_mode(tmp_path / "mic.json", "off")
    gate.update('{"state": "open"}')
    assert not gate.heard()
    write_mode(tmp_path / "mic.json", "auto")
    assert gate.heard()


def test_bad_input_is_ignored(tmp_path):
    gate = _gate(tmp_path)
    gate.update("not json")
    gate.update('{"state": "sideways"}')
    gate.update('{"state": "none", "error": "not trusted"}')

    assert gate.state == "none" and gate.error == "not trusted"
    (tmp_path / "mic.json").write_text("{broken")
    assert read_mode(tmp_path / "mic.json") == "auto"
    with pytest.raises(ValueError):
        write_mode(tmp_path / "mic.json", "loud")


def test_the_helper_stream_is_followed_and_stopped(tmp_path):
    process = Process([{"state": "muted", "app": "Google Meet"}, {"state": "open", "app": "Google Meet"}])
    gate = MicGate(tmp_path / "mic.json", spawn=lambda: process, clock=Clock())

    assert gate.start()
    gate._thread.join(timeout=2)
    gate.stop()

    assert gate.state == "open" and gate.app == "Google Meet" and process.stopped


def test_without_a_helper_the_gate_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(mic_gate, "helper_path", lambda: None)
    gate = MicGate(tmp_path / "mic.json")

    assert not gate.start()
    assert "buddy-mic-state" in gate.error
    gate.stop()  # nothing to stop


# ------------------------------------------------------------------- listener


def _me(text):
    return TranscriptEvent("ME", text, True, "u")


async def test_muted_lines_are_not_kept_but_heard_ones_are(tmp_path):
    clock = Clock()
    daemon, _ = _daemon(tmp_path, clock, mic_follows_mute=True, echo_window_seconds=0)
    gates = []

    def factory():
        gate = _gate(tmp_path, clock=clock)
        gates.append(gate)
        return gate

    daemon.mic_gate_factory = factory
    await daemon.start_meeting("requested")
    gate = gates[0]
    gate.update('{"state": "muted", "app": "Microsoft Teams"}')
    await daemon.on_final(_me("talking to someone in the room"))
    gate.update('{"state": "open", "app": "Microsoft Teams"}')
    await daemon.on_final(_me("I can take that action item"))
    meeting = daemon.storage.directory
    await daemon.stop_meeting("requested")

    text = (meeting / "transcript.txt").read_text(encoding="utf-8")
    assert "I can take that action item" in text
    assert "someone in the room" not in text
    metadata = json.loads((meeting / "metadata.json").read_text())
    assert metadata["mic"]["dropped"] == 1 and metadata["mic"]["app"] == "Microsoft Teams"
    assert daemon.mic_gate is None


async def test_a_missing_helper_is_said_once_and_lines_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(mic_gate, "helper_path", lambda: None)
    daemon, _ = _daemon(tmp_path, mic_follows_mute=True, echo_window_seconds=0)
    await daemon.start_meeting("requested")
    await daemon.on_final(_me("still kept"))

    notice = [e for e in _events(daemon) if e["type"] == "NOTICE"][0]["text"]
    assert "buddy-mic-state" in notice
    assert "still kept" in daemon.storage.transcript_txt.read_text(encoding="utf-8")


async def test_off_by_default_the_microphone_is_always_kept(tmp_path):
    daemon, _ = _daemon(tmp_path, echo_window_seconds=0)
    await daemon.start_meeting("requested")
    await daemon.on_final(_me("kept as before"))

    assert daemon.mic_gate is None
    assert "kept as before" in daemon.storage.transcript_txt.read_text(encoding="utf-8")


def test_buddy_mic_sets_and_shows_the_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_runtime_dir", lambda: tmp_path / "runtime")
    runner = CliRunner()

    assert "Microphone: auto" in runner.invoke(cli.app, ["mic"]).stdout
    assert "Microphone: off" in runner.invoke(cli.app, ["mic", "off"]).stdout
    assert read_mode(tmp_path / "runtime" / "listen" / "mic.json") == "off"
    assert runner.invoke(cli.app, ["mic", "loud"]).exit_code == 2


async def test_the_window_learns_the_microphone_state_when_it_changes(tmp_path):
    clock = Clock()
    daemon, _ = _daemon(tmp_path, clock, mic_follows_mute=True, echo_window_seconds=0)
    gate = _gate(tmp_path, clock=clock)
    daemon.mic_gate_factory = lambda: gate
    await daemon.start_meeting("requested")
    gate.update('{"state": "open", "app": "Google Meet"}')

    await daemon.tick()
    state = json.loads(daemon.listen_file.read_text())

    assert state["mic"] == {"mode": "auto", "state": "open", "app": "Google Meet", "heard": True}


def test_the_window_button_sets_the_mode(tmp_path):
    from meeting_agent.window import WindowApi

    api = WindowApi(tmp_path, view=None)

    assert api.set_mic("off") == {"ok": True, "mode": "off"}
    assert read_mode(tmp_path / "listen" / "mic.json") == "off"
    assert api.set_mic("loud")["ok"] is False
