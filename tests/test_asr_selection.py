import pytest

from meeting_agent.asr import selection as selection_module
from meeting_agent.asr.selection import select_asr
from meeting_agent.config import Settings


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    for name in ("OPENAI_API_KEY", "BUDDY_ALLOW_CLOUD_AUDIO", "MEETING_AGENT_ALLOW_CLOUD_AUDIO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(selection_module, "has_nvidia", lambda: False)


def test_auto_falls_back_to_cpu_without_key_or_gpu():
    result = select_asr(Settings())

    assert result.mode == "local-cpu"
    assert result.device == "cpu"


def test_auto_selects_gpu_when_available(monkeypatch):
    monkeypatch.setattr(selection_module, "has_nvidia", lambda: True)

    assert select_asr(Settings()).mode == "local-gpu"


def test_cloud_requires_both_key_and_explicit_audio_consent(monkeypatch):
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        select_asr(Settings(asr_mode="cloud-fast"))

    monkeypatch.setenv("OPENAI_API_KEY", "x")
    with pytest.raises(RuntimeError, match="explicit consent"):
        select_asr(Settings(asr_mode="cloud-fast"))

    monkeypatch.setenv("BUDDY_ALLOW_CLOUD_AUDIO", "true")
    assert select_asr(Settings(asr_mode="cloud-fast")).mode == "cloud-fast"


def test_auto_never_uploads_audio_on_key_alone(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    assert select_asr(Settings()).mode == "local-cpu"


def test_explicit_gpu_request_fails_loudly_without_hardware():
    with pytest.raises(RuntimeError, match="no NVIDIA GPU"):
        select_asr(Settings(asr_mode="local-gpu"))


def test_multilingual_model_is_used_for_non_english():
    assert select_asr(Settings(language="pt")).model == Settings().asr.local_model_multilingual


def test_vad_calibration_survives_a_speaker_who_is_already_talking():
    from meeting_agent.asr.local import calibrate_threshold

    # Every calibration frame is loud speech; the gate must still sit below it.
    speech = [0.20, 0.28, 0.19, 0.06, 0.32, 0.23, 0.23, 0.15, 0.28, 0.24]
    for speaker in ("ME", "REMOTE"):
        threshold = calibrate_threshold(speech, speaker, 0.012)
        assert threshold <= 0.012 * 8
        # Most of that speech has to clear the gate, or the stream stays mute.
        assert sum(level >= threshold for level in speech) >= 8


def test_vad_calibration_on_silence_keeps_the_configured_floor():
    from meeting_agent.asr.local import calibrate_threshold

    silence = [0.0002, 0.0003, 0.0003, 0.0004, 0.0002, 0.0003, 0.0002, 0.0004, 0.0003, 0.0002]
    assert calibrate_threshold(silence, "ME", 0.012) == 0.012
    assert calibrate_threshold(silence, "REMOTE", 0.012) == 0.012
