import pytest

from meeting_agent.asr import selection as selection_module
from meeting_agent.asr.selection import select_asr
from meeting_agent.config import Settings


@pytest.fixture(autouse=True)
def _no_gpu(monkeypatch):
    monkeypatch.setattr(selection_module, "has_nvidia", lambda: False)


def test_auto_falls_back_to_cpu_without_a_gpu():
    result = select_asr(Settings())

    assert result.mode == "local-cpu"
    assert result.device == "cpu"


def test_auto_selects_gpu_when_available(monkeypatch):
    monkeypatch.setattr(selection_module, "has_nvidia", lambda: True)

    assert select_asr(Settings()).mode == "local-gpu"


def test_every_selectable_mode_is_local():
    """Whatever the configuration, audio is transcribed on this machine."""
    for mode in ("auto", "local-cpu"):
        assert select_asr(Settings(asr_mode=mode)).device == "cpu"


def test_explicit_gpu_request_fails_loudly_without_hardware():
    with pytest.raises(RuntimeError, match="no NVIDIA GPU"):
        select_asr(Settings(asr_mode="local-gpu"))


def test_multilingual_model_is_used_for_non_english():
    assert select_asr(Settings(language="pt")).model == Settings().asr.local_model_multilingual
