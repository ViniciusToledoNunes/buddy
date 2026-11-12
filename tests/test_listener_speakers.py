import asyncio
import json

import numpy as np

from meeting_agent import active_speaker
from meeting_agent.events import SpeechEvent, TranscriptEvent
from meeting_agent.speakers import VOICE_MODEL_FILE, VoiceBook

from test_listener import Clock, _daemon, _events


def _voice(seed):
    vector = np.random.default_rng(seed).normal(size=256)
    return vector / np.linalg.norm(vector)


def _remote(text, at, embedding=None, length=4.0):
    return TranscriptEvent("REMOTE", text, True, "u", embedding=embedding, spoken_from=at, spoken_to=at + length)


def _with_model(daemon):
    path = daemon.runtime_dir / "models" / VOICE_MODEL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"onnx")


def _screen(daemon, names):
    """The screen outlines these people, one capture after another."""
    shown = iter(names)

    def capture(path, max_width):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg")
        return True

    daemon.screen_capture = capture
    daemon.read_speaker = lambda path: next(shown)


async def _look(daemon, at):
    daemon.on_speech(SpeechEvent("REMOTE", at))
    if daemon._look is not None:
        await daemon._look


def _transcript(daemon):
    return daemon.storage.transcript_txt.read_text(encoding="utf-8")


async def test_the_screen_names_the_remote_line(tmp_path, monkeypatch):
    monkeypatch.setattr(active_speaker, "available", lambda: True)
    clock = Clock()
    daemon, _ = _daemon(tmp_path, clock, speaker_names="screen", batch_min_seconds=30, batch_pause_seconds=2)
    await daemon.start_meeting("requested")
    _screen(daemon, ["Dana Reyes"])

    await _look(daemon, 1_000.5)
    await daemon.on_final(_remote("The rollout moved to Tuesday.", 1_000.0))
    clock.now += 40
    await daemon.tick()

    assert "REMOTE (Dana Reyes): The rollout moved to Tuesday." in _transcript(daemon)
    batch = [e for e in _events(daemon) if e["type"] == "MEETING_BATCH"][0]
    assert batch["lines"] == ["REMOTE (Dana Reyes): The rollout moved to Tuesday."]
    assert not any(daemon.storage.directory.joinpath(".speaker").glob("*"))  # captures read, then deleted


async def test_looks_at_the_screen_are_spaced_and_only_for_the_other_side(tmp_path, monkeypatch):
    monkeypatch.setattr(active_speaker, "available", lambda: True)
    daemon, _ = _daemon(tmp_path, speaker_names="screen", speaker_screen_seconds=2)
    await daemon.start_meeting("requested")
    _screen(daemon, ["Dana Reyes", "Chidi Okafor", "Mae Lindqvist"])

    await _look(daemon, 10.0)
    await _look(daemon, 11.0)  # too soon
    daemon.on_speech(SpeechEvent("ME", 13.0))  # the user's own speech is not looked at
    await _look(daemon, 12.5)

    assert daemon._sightings == [(10.0, "Dana Reyes"), (12.5, "Chidi Okafor")]


async def test_a_failed_look_loses_only_that_sighting(tmp_path, monkeypatch):
    monkeypatch.setattr(active_speaker, "available", lambda: True)
    daemon, _ = _daemon(tmp_path, speaker_names="screen")
    await daemon.start_meeting("requested")

    def broken(path):
        raise RuntimeError("helper crashed")

    daemon.screen_capture = lambda path, width: True
    daemon.read_speaker = broken
    await _look(daemon, 5.0)

    assert daemon._sightings == []


async def test_voices_are_named_by_the_screen_remembered_and_reported_at_the_end(tmp_path, monkeypatch):
    monkeypatch.setattr(active_speaker, "available", lambda: True)
    clock = Clock()
    daemon, _ = _daemon(tmp_path, clock, speaker_names="both", remember_voices=True)
    _with_model(daemon)
    await daemon.start_meeting("requested")
    _screen(daemon, ["Dana Reyes"] * 3)
    dana, other = _voice(1), _voice(2)

    for n in range(3):
        at = 2_000.0 + 10 * n
        await _look(daemon, at + 0.5)
        await daemon.on_final(_remote(f"dana line {n}", at, dana))
    await daemon.on_final(_remote("dana with the screen shared", 3_000.0, dana))
    await daemon.on_final(_remote("someone else", 3_010.0, other))
    await daemon.on_final(_remote("someone else again", 3_020.0, other))
    meeting = daemon.storage.directory
    await daemon.stop_meeting("requested")

    text = (meeting / "transcript.txt").read_text(encoding="utf-8")
    assert "REMOTE (Dana Reyes): dana with the screen shared" in text
    assert "REMOTE (voice B): someone else again" in text
    end = [e for e in _events(daemon) if e["type"] == "MEETING_END"][0]
    assert end["speakers"] == ["Dana Reyes"]
    assert end["unnamed_voices"] == ["B"]
    assert end["voices_learned"] == ["Dana Reyes"]
    assert VoiceBook(daemon.voice_book_path).names() == ["Dana Reyes"]
    assert [v["voice"] for v in json.loads((meeting / "voices.json").read_text())["voices"]] == ["A", "B"]
    metadata = json.loads((meeting / "metadata.json").read_text())
    assert metadata["speakers"][0]["name"] == "Dana Reyes"
    lines = [json.loads(line) for line in (meeting / "transcript.jsonl").read_text().splitlines()]
    assert lines[-1]["voice"] == "B" and "embedding" not in lines[-1]


async def test_missing_pieces_are_said_once_and_the_meeting_still_records(tmp_path, monkeypatch):
    monkeypatch.setattr(active_speaker, "available", lambda: False)
    daemon, _ = _daemon(tmp_path, speaker_names="both")
    await daemon.start_meeting("requested")
    await daemon.on_final(_remote("plain line", 1.0, _voice(1)))

    notice = [e for e in _events(daemon) if e["type"] == "NOTICE"][0]["text"]
    assert "screen cannot be read" in notice and "buddy voices setup" in notice
    assert daemon.speakers is None
    assert "] REMOTE: plain line" in _transcript(daemon)


async def test_off_by_default_nothing_changes(tmp_path):
    daemon, _ = _daemon(tmp_path)
    await daemon.start_meeting("requested")
    daemon.on_speech(SpeechEvent("REMOTE", 1.0))
    await daemon.on_final(_remote("plain line", 1.0, _voice(1)))
    await daemon.stop_meeting("requested")

    end = [e for e in _events(daemon) if e["type"] == "MEETING_END"][0]
    assert daemon._look is None
    assert "speakers" not in end and "unnamed_voices" not in end


async def test_voice_only_groups_lines_without_the_screen(tmp_path):
    daemon, _ = _daemon(tmp_path, speaker_names="voice")
    _with_model(daemon)
    await daemon.start_meeting("requested")

    await daemon.on_final(_remote("first", 1.0, _voice(1)))
    await daemon.on_final(_remote("second", 5.0, _voice(2)))
    await daemon.on_final(_remote("too short for a voice", 9.0, None))

    text = _transcript(daemon)
    assert "REMOTE (voice A): first" in text and "REMOTE (voice B): second" in text
    assert "] REMOTE: too short for a voice" in text


def test_transient_fields_are_never_written():
    event = TranscriptEvent("REMOTE", "hi", True, "u", embedding=np.ones(3), spoken_from=1.0, spoken_to=2.0)

    assert set(event.to_dict()) == {
        "speaker", "text", "final", "utterance_id", "timestamp", "start_seconds", "end_seconds", "latency_seconds",
    }
    event.name, event.voice, event.name_sure = "Dana Reyes", "A", True
    assert event.to_dict()["name"] == "Dana Reyes" and event.who == "REMOTE (Dana Reyes)"
    event.name = None
    assert event.who == "REMOTE (voice A)"
