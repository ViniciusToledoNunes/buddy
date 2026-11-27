import re

from meeting_agent import cursor_agent
from meeting_agent.config import load_settings
from meeting_agent.cursor_agent import parse_models
from meeting_agent.harness import HARNESSES
from meeting_agent.live import LiveView
from meeting_agent.window import PAGE, WindowApi

# The start and end of what `cursor-agent models` printed on 2026-11-12.
CURSOR_MODELS = """Available models

auto - Auto (default)
gpt-5.3-codex-low - Codex 5.3 Low
composer-2.5 - Composer 2.5
claude-opus-5-thinking-high - Claude Opus 5 1M Thinking

Tip: use --model <id> (or /model <id> in interactive mode) to switch. Parameterized models also accept quoted overrides, e.g. --model 'claude-opus-4-8[context=1m,effort=high,fast=false]'.
"""


def _api(tmp_path, harness="claude-code", model="opus"):
    config = tmp_path / "config.yaml"
    config.write_text(
        f"language: en\nbrain:\n  harness: {harness}   # claude-code | cursor\n  model: '{model}'\n  workdir: '{tmp_path}'\n",
        encoding="utf-8",
    )
    return WindowApi(tmp_path / "runtime", LiveView(tmp_path / "runtime", home=tmp_path), config_file=config), config


def test_the_panel_shows_the_brain_in_use_and_the_choices(tmp_path, monkeypatch):
    from meeting_agent import claude_code

    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")
    api, _ = _api(tmp_path)

    brain = api.get_brain()

    assert brain["ok"] and (brain["harness"], brain["model"]) == ("claude-code", "opus")
    assert [h["name"] for h in brain["harnesses"]] == ["claude-code", "cursor"]
    assert brain["readiness"]["state"] in {"ok", "warning"}


def test_saving_switches_the_harness_and_model_in_the_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor_agent, "cursor_agent_binary", lambda: "cursor-agent")
    api, config = _api(tmp_path)

    saved = api.set_brain("cursor", " composer-2.5 ")

    assert saved["ok"] and (saved["harness"], saved["model"]) == ("cursor", "composer-2.5")
    assert saved["readiness"]["state"] == "ok"
    assert "# claude-code | cursor" in config.read_text(encoding="utf-8")
    assert (load_settings(config).brain.harness, load_settings(config).brain.model) == ("cursor", "composer-2.5")


def test_a_bad_choice_is_refused_and_the_config_is_left_alone(tmp_path):
    api, config = _api(tmp_path)
    before = config.read_text(encoding="utf-8")

    assert api.set_brain("emacs", "")["ok"] is False
    assert api.set_brain("cursor", "two words")["ok"] is False
    assert api.set_brain("cursor", "x" * 201)["ok"] is False

    assert config.read_text(encoding="utf-8") == before


def test_the_models_are_listed_once_per_harness(tmp_path, monkeypatch):
    calls = []

    def listed():
        calls.append(1)
        return parse_models(CURSOR_MODELS)

    monkeypatch.setattr(cursor_agent, "list_models", listed)
    api, _ = _api(tmp_path)

    first = api.brain_models("cursor")
    api.brain_models("cursor")

    assert first["ok"] and first["models"][0] == {"id": "auto", "label": "Auto (default)"}
    assert len(calls) == 1
    assert [m["id"] for m in api.brain_models("claude-code")["models"]] == ["opus", "sonnet", "haiku"]
    assert api.brain_models("emacs")["ok"] is False


def test_cursors_model_list_keeps_only_the_model_lines():
    assert parse_models(CURSOR_MODELS) == [
        ("auto", "Auto (default)"),
        ("gpt-5.3-codex-low", "Codex 5.3 Low"),
        ("composer-2.5", "Composer 2.5"),
        ("claude-opus-5-thinking-high", "Claude Opus 5 1M Thinking"),
    ]


def test_no_cursor_cli_means_no_models(monkeypatch):
    monkeypatch.setattr(cursor_agent, "cursor_agent_binary", lambda: None)
    assert HARNESSES["cursor"].models() == []


def test_every_call_the_page_makes_is_a_window_method():
    called = set(re.findall(r'call\("(\w+)"', PAGE))

    assert {"get_brain", "brain_models", "set_brain"} <= called
    assert all(callable(getattr(WindowApi, name, None)) for name in called)
    assert 'id="brain-button"' in PAGE and 'id="brain-panel"' in PAGE
