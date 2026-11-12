import json

import numpy as np
import pytest
from typer.testing import CliRunner

from meeting_agent import cli, speakers
from meeting_agent.config import Settings
from meeting_agent.speakers import SpeakerTracker, VoiceBook


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    path = tmp_path / "runtime"
    monkeypatch.setattr(cli, "_runtime_dir", lambda: path)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings.model_validate({"meetings_dir": str(tmp_path / "meetings")}))
    return path


def _unit(seed):
    vector = np.random.default_rng(seed).normal(size=256)
    return vector / np.linalg.norm(vector)


def test_list_and_forget_remembered_voices(runtime):
    book = VoiceBook(runtime / "voices.json")
    book.learn("Dana Reyes", _unit(1), 4)
    book.learn("Chidi Okafor", _unit(2), 2)
    book.save()
    runner = CliRunner()

    listed = runner.invoke(cli.app, ["voices", "list"])
    assert "Dana Reyes" in listed.stdout and "Chidi Okafor" in listed.stdout

    assert runner.invoke(cli.app, ["voices", "forget"]).exit_code == 2  # neither a name nor --all
    assert "Forgot: Dana Reyes" in runner.invoke(cli.app, ["voices", "forget", "Dana Reyes"]).stdout
    assert "Nothing to forget" in runner.invoke(cli.app, ["voices", "forget", "Nobody"]).stdout
    assert "Forgot: Chidi Okafor" in runner.invoke(cli.app, ["voices", "forget", "--all"]).stdout
    assert "No voices remembered" in runner.invoke(cli.app, ["voices", "list"]).stdout


def test_name_a_voice_from_a_past_meeting(runtime, tmp_path):
    meeting = tmp_path / "meetings" / "2026-01-05_100000"
    meeting.mkdir(parents=True)
    tracker = SpeakerTracker()
    tracker.observe(_unit(3), [])
    (meeting / "voices.json").write_text(json.dumps(tracker.state()))
    runner = CliRunner()

    named = runner.invoke(cli.app, ["voices", "name", "2026-01-05_100000", "a", "Mae Lindqvist"])
    missing = runner.invoke(cli.app, ["voices", "name", "2026-01-05_100000", "q", "Nobody"])

    assert named.exit_code == 0 and "remembered as Mae Lindqvist" in named.stdout
    assert missing.exit_code == 1
    assert VoiceBook(runtime / "voices.json").names() == ["Mae Lindqvist"]


def test_setup_downloads_the_model_into_the_runtime_folder(runtime, monkeypatch):
    fetched = []
    monkeypatch.setattr(speakers, "download_model", lambda directory: fetched.append(directory) or directory / "m.onnx")

    result = CliRunner().invoke(cli.app, ["voices", "setup"])

    assert result.exit_code == 0 and fetched == [runtime / "models"]


def test_setup_leaves_a_configured_model_alone(runtime, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_settings", lambda: Settings.model_validate({"listen": {"voice_model": str(tmp_path / "own.onnx")}}))
    monkeypatch.setattr(speakers, "download_model", lambda directory: pytest.fail("should not download"))

    result = CliRunner().invoke(cli.app, ["voices", "setup"])

    assert "listen.voice_model is set" in result.stdout


def test_setup_explains_a_failed_download(runtime, monkeypatch):
    def offline(directory):
        raise OSError("certificate verify failed")

    monkeypatch.setattr(speakers, "download_model", offline)

    result = CliRunner().invoke(cli.app, ["voices", "setup"])

    assert result.exit_code == 1
    assert "Could not download" in result.stdout and "voice_model" in result.stdout
