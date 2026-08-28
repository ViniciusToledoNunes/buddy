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


def test_openai_is_not_a_selectable_provider():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(llm_provider="openai")


def test_tiered_claude_models_are_configured():
    copilot = Settings().copilot
    assert copilot.anthropic_model == "claude-haiku-4-5"
    assert copilot.deep_model == "claude-opus-5"


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
