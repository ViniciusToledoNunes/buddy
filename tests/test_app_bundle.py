import os
import plistlib
import sys

import pytest
from typer.testing import CliRunner

from meeting_agent import cli
from meeting_agent.app_bundle import build_app
from meeting_agent.window import FRONT_FLAG, bring_to_front


def test_the_app_runs_buddy_open_through_a_login_shell(tmp_path):
    buddy = tmp_path / "my checkout" / ".venv" / "bin" / "buddy"

    app = build_app(buddy, tmp_path / "Buddy.app", icon_png=tmp_path / "missing.png")

    plist = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert plist["CFBundleExecutable"] == "buddy-launcher" and plist["CFBundleName"] == "Buddy"
    launcher = app / "Contents" / "MacOS" / "buddy-launcher"
    assert os.access(launcher, os.X_OK)
    text = launcher.read_text(encoding="utf-8")
    # The terminal's PATH and variables (Finder gives a bare PATH, and the brain runs `claude`).
    assert f"-l -i -c '\"{buddy}\" open'" in text


def test_rebuilding_replaces_the_old_app(tmp_path):
    dest = tmp_path / "Buddy.app"
    build_app(tmp_path / "a" / "buddy", dest, icon_png=tmp_path / "missing.png")
    (dest / "Contents" / "stale").write_text("x")

    build_app(tmp_path / "b" / "buddy", dest, icon_png=tmp_path / "missing.png")

    assert not (dest / "Contents" / "stale").exists()
    assert str(tmp_path / "b" / "buddy") in (dest / "Contents" / "MacOS" / "buddy-launcher").read_text()


def test_a_path_with_a_single_quote_is_refused(tmp_path):
    with pytest.raises(ValueError):
        build_app(tmp_path / "it's" / "buddy", tmp_path / "Buddy.app", icon_png=tmp_path / "missing.png")


@pytest.mark.skipif(sys.platform != "darwin", reason="iconutil and sips are macOS tools")
def test_the_app_gets_buddys_icon(tmp_path):
    app = build_app(tmp_path / "buddy", tmp_path / "Buddy.app")

    assert (app / "Contents" / "Resources" / "Buddy.icns").stat().st_size > 10_000


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    path = tmp_path / "runtime"
    monkeypatch.setattr(cli, "_runtime_dir", lambda: path)
    return path


def test_open_starts_buddy_and_its_window_when_off(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr("meeting_agent.listener.listener_state", lambda r: None)
    monkeypatch.setattr("meeting_agent.window.window_state", lambda r: None)
    monkeypatch.setattr(cli, "_start_listener", lambda r: calls.append("listener") or True)
    monkeypatch.setattr(cli, "_open_window", lambda r: calls.append("window"))

    result = CliRunner().invoke(cli.app, ["open"])

    assert result.exit_code == 0 and calls == ["listener", "window"]


def test_open_brings_an_open_window_to_the_front(runtime, monkeypatch):
    monkeypatch.setattr("meeting_agent.listener.listener_state", lambda r: {"pid": 1})
    monkeypatch.setattr("meeting_agent.window.window_state", lambda r: {"pid": 2})
    monkeypatch.setattr(cli, "_start_listener", lambda r: pytest.fail("Buddy is already on"))

    result = CliRunner().invoke(cli.app, ["open"])

    assert result.exit_code == 0 and (runtime / FRONT_FLAG).exists()


def test_open_reopens_the_window_of_a_running_buddy(runtime, monkeypatch):
    opened = []
    monkeypatch.setattr("meeting_agent.listener.listener_state", lambda r: {"pid": 1})
    monkeypatch.setattr("meeting_agent.window.window_state", lambda r: None)
    monkeypatch.setattr(cli, "_open_window", lambda r: opened.append(r))

    CliRunner().invoke(cli.app, ["open"])

    assert opened == [runtime]


def test_bring_to_front_leaves_a_flag_for_the_window(tmp_path):
    bring_to_front(tmp_path / "rt")

    assert (tmp_path / "rt" / FRONT_FLAG).exists()
