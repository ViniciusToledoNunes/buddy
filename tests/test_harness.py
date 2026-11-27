from meeting_agent import claude_code
from meeting_agent.claude_code import ClaudeCodeProvider
from meeting_agent.config import Settings
from meeting_agent.harness import HARNESSES, ClaudeCodeHarness, harness_for


def _settings(tmp_path, **brain):
    return Settings.model_validate({"llm_provider": "harness", "brain": {"workdir": str(tmp_path), **brain}})


def test_the_brain_block_selects_the_harness_and_its_model(tmp_path):
    settings = _settings(tmp_path, model="sonnet", effort="low")
    harness = harness_for(settings)
    assert isinstance(harness, ClaudeCodeHarness) and HARNESSES["claude-code"] is harness

    provider = harness.provider(settings, tmp_path / "meetings")
    assert isinstance(provider, ClaudeCodeProvider)
    assert provider.workdir == tmp_path
    command = provider.command("system")
    assert command[command.index("--model") + 1] == "sonnet"
    assert command[command.index("--effort") + 1] == "low"


def test_a_harness_left_on_its_defaults_passes_no_model(tmp_path):
    command = harness_for(_settings(tmp_path)).provider(_settings(tmp_path)).command("system")
    assert "--model" not in command and "--effort" not in command


def test_llm_provider_harness_runs_the_brain_when_it_can(tmp_path, monkeypatch):
    from meeting_agent.copilot import choose_provider

    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")
    assert isinstance(choose_provider(_settings(tmp_path), local_asr=True), ClaudeCodeProvider)

    monkeypatch.setattr(claude_code, "claude_binary", lambda: None)
    assert choose_provider(_settings(tmp_path), local_asr=True) is None


def test_the_auto_brain_runs_the_configured_harness(tmp_path):
    from meeting_agent.autobrain import AutoBrain

    brain = AutoBrain(_settings(tmp_path, model="opus"), tmp_path / "meetings")
    assert isinstance(brain.provider, ClaudeCodeProvider)
    assert brain.provider.settings.brain.model == "opus"


def test_the_doctor_reads_the_harness_readiness(tmp_path, monkeypatch):
    from meeting_agent.doctor import brain_check

    monkeypatch.setattr(claude_code, "claude_binary", lambda: "claude")
    missing = brain_check(_settings(tmp_path / "gone"))
    assert missing.state == "failed" and "brain.workdir" in missing.detail

    (tmp_path / "CLAUDE.md").write_text("context", encoding="utf-8")
    ready = brain_check(_settings(tmp_path))
    assert ready.state == "ok" and "CLAUDE.md" in ready.detail
