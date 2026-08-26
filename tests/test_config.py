from meeting_agent.config import Settings


def test_default_settings_are_local_safe(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = Settings()
    assert settings.asr_mode == "auto"
    assert settings.save_audio is False
    assert settings.audio.sample_rate == 24000
