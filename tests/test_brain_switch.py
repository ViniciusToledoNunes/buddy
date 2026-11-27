import pytest
from typer.testing import CliRunner

from meeting_agent import claude_code, cursor_agent
from meeting_agent.cli import app
from meeting_agent.config import Settings, choose_brain, load_settings
from meeting_agent.listener import ListenDaemon
from test_listener import Clock, FakeStream, _events, _said

CONFIG = """language: en
llm_provider: auto           # harness, auto, openai
# The agent Buddy runs headless.
brain:
  harness: claude-code       # claude-code | cursor
  model: 'opus'              # empty uses the harness's default
  workdir: ''
  cursor:
    force: true
copilot:
  enabled: true
"""


def test_switching_rewrites_only_the_choice_and_keeps_the_comments(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")

    backup = choose_brain("cursor", "composer-2.5", path)

    text = path.read_text(encoding="utf-8")
    assert "  harness: 'cursor'       # claude-code | cursor" in text
    assert "  model: 'composer-2.5'              # empty uses the harness's default" in text
    assert "# The agent Buddy runs headless." in text and "    force: true" in text
    assert backup is not None and backup.read_text(encoding="utf-8") == CONFIG
    brain = load_settings(path).brain
    assert (brain.harness, brain.model, brain.cursor.force) == ("cursor", "composer-2.5", True)


def test_switching_back_clears_the_other_harness_model(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")

    choose_brain("cursor", "composer-2.5", path)
    choose_brain("claude-code", "", path)

    brain = load_settings(path).brain
    assert (brain.harness, brain.model) == ("claude-code", "")


def test_a_config_without_a_brain_block_gets_one_that_beats_the_legacy_keys(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("llm_provider: claude-code\ncopilot:\n  claude_model: opus\n", encoding="utf-8")

    assert choose_brain("cursor", "", path) is not None

    settings = load_settings(path)
    assert (settings.brain.harness, settings.brain.model) == ("cursor", "")
    assert settings.llm_provider == "harness"


def test_an_unknown_harness_leaves_the_file_alone(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")

    with pytest.raises(ValueError):
        choose_brain("emacs", "", path)

    assert path.read_text(encoding="utf-8") == CONFIG
    assert list(tmp_path.glob("config.yaml.bak-*")) == []


def test_buddy_brain_switches_and_shows_the_brain(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG.replace("workdir: ''", f"workdir: '{tmp_path}'"), encoding="utf-8")
    monkeypatch.setenv("BUDDY_CONFIG", str(path))
    monkeypatch.setattr(cursor_agent, "cursor_agent_binary", lambda: "cursor-agent")
    runner = CliRunner()

    switched = runner.invoke(app, ["brain", "cursor", "--model", "composer-2.5"])
    assert switched.exit_code == 0, switched.output
    assert "Brain: Cursor (cursor), model: composer-2.5" in switched.output
    assert "Previous configuration saved to" in switched.output

    shown = runner.invoke(app, ["brain"])
    assert shown.exit_code == 0 and "Brain: Cursor" in shown.output

    refused = runner.invoke(app, ["brain", "emacs"])
    assert refused.exit_code == 1 and "Unknown harness" in refused.output
    assert load_settings(path).brain.harness == "cursor"


def _daemon(tmp_path, brain_settings):
    settings = Settings.model_validate({
        "meetings_dir": str(tmp_path / "meetings"),
        "listen": {"sounds": False, "echo_window_seconds": 0},
        "brain": {"workdir": str(tmp_path)},
    })
    daemon = ListenDaemon(settings, tmp_path / "runtime", stream_factory=lambda s: FakeStream(s, []),
                          clock=Clock(), brain_settings=brain_settings)
    daemon.chime = lambda kind: None
    daemon.claim()
    return daemon


async def test_a_meeting_starts_the_brain_chosen_since_the_listener_started(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor_agent, "cursor_agent_binary", lambda: None)
    chosen = Settings.model_validate({"brain": {"harness": "cursor", "workdir": str(tmp_path)}})
    daemon = _daemon(tmp_path, lambda: chosen)
    await daemon.start_meeting("test")

    await daemon.on_final(_said("REMOTE", "When can we ship the vendor page?"))

    assert any("built-in brain is unavailable (Cursor)" in e.get("text", "") for e in _events(daemon))


async def test_a_config_that_cannot_be_read_keeps_the_brain_loaded_at_start(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "claude_binary", lambda: None)

    def broken():
        raise ValueError("mid-edit")

    daemon = _daemon(tmp_path, broken)
    await daemon.start_meeting("test")

    await daemon.on_final(_said("REMOTE", "When can we ship the vendor page?"))

    texts = [e.get("text", "") for e in _events(daemon)]
    assert any("Could not reread the brain settings" in text and "mid-edit" in text for text in texts)
    assert any("built-in brain is unavailable (Claude Code)" in text for text in texts)
