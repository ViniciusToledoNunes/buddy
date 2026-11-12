from meeting_agent.config import Settings


def test_default_settings_are_local_safe(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings()
    assert settings.asr_mode == "auto"
    assert settings.save_audio is False
    assert settings.audio.sample_rate == 24000


def test_transcription_is_always_local():
    """Cloud ASR is gone, so no configuration can send audio off the machine."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(asr_mode="cloud-fast")


def test_the_brain_defaults_to_claude_code_with_its_own_defaults():
    brain = Settings().brain
    assert (brain.harness, brain.model, brain.effort, brain.workdir) == ("claude-code", "", "", "")


def test_a_configuration_from_before_the_harness_choice_still_names_its_brain(tmp_path):
    """`llm_provider: claude-code` and the `copilot.claude_*` keys are what every existing
    config.yaml says; they must keep selecting the same brain with the same settings."""
    settings = Settings.model_validate(
        {
            "llm_provider": "claude-code",
            "copilot": {"claude_workdir": str(tmp_path), "claude_model": "opus", "claude_effort": "medium"},
        }
    )
    assert settings.llm_provider == "harness"
    assert settings.brain.harness == "claude-code"
    assert (settings.brain.workdir, settings.brain.model, settings.brain.effort) == (str(tmp_path), "opus", "medium")
    assert settings.brain.directory() == tmp_path


def test_the_brain_block_wins_over_its_legacy_twin():
    settings = Settings.model_validate(
        {"brain": {"model": "sonnet"}, "copilot": {"claude_model": "opus", "claude_effort": "high"}}
    )
    assert settings.brain.model == "sonnet"
    assert settings.brain.effort == "high"  # not set in the block, so the legacy key still counts


def test_an_unknown_harness_is_rejected():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings.model_validate({"brain": {"harness": "notepad"}})


def test_an_empty_workdir_means_home():
    from pathlib import Path

    assert Settings().brain.directory() == Path.home()


def test_workspace_header_is_sent_only_when_configured(monkeypatch):
    """Identity-linked keys are rejected with HTTP 400 unless the request names a
    workspace; plain workspace keys must not receive a stray header."""
    from meeting_agent.config import anthropic_client_options, anthropic_headers

    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    assert anthropic_headers() == {}
    assert anthropic_client_options(30.0) == {"timeout": 30.0}

    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "  wrkspc_example  ")
    assert anthropic_headers() == {"anthropic-workspace-id": "wrkspc_example"}
    assert anthropic_client_options(30.0)["default_headers"] == {"anthropic-workspace-id": "wrkspc_example"}


def test_env_files_are_sourced_and_expand_home(tmp_path, monkeypatch):
    """A credential a wrapper script sources per command and never exports looks absent
    to a plain environment read."""
    from meeting_agent.config import Settings, load_env_files

    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "team-jira.env").write_text("JIRA_API_TOKEN=from-file\n", encoding="utf-8")

    loaded = load_env_files(Settings(env_files=["~/.config/team-jira.env", "~/.config/absent.env"]))

    import os

    assert os.getenv("JIRA_API_TOKEN") == "from-file"
    assert len(loaded) == 1  # the missing one is skipped, not an error
