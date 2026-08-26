from meeting_agent.config import Settings, cloud_audio_allowed


def test_default_settings_are_local_safe(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = Settings()
    assert settings.asr_mode == "auto"
    assert settings.save_audio is False
    assert settings.audio.sample_rate == 24000


def test_buddy_cloud_audio_opt_in(monkeypatch):
    monkeypatch.delenv("MEETING_AGENT_ALLOW_CLOUD_AUDIO", raising=False)
    monkeypatch.setenv("BUDDY_ALLOW_CLOUD_AUDIO", "true")
    assert cloud_audio_allowed() is True
