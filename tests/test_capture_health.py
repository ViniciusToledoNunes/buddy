import numpy as np

from meeting_agent.capture.base import SilenceWatchdog


def _noise(level: float, size: int = 240) -> np.ndarray:
    return np.full(size, level, dtype=np.float32)


def test_live_microphone_noise_never_trips_the_watchdog():
    watchdog = SilenceWatchdog(timeout_seconds=10.0)
    watchdog.reset(0.0)

    assert watchdog.observe(_noise(1e-3), 5.0) is False
    assert watchdog.observe(_noise(1e-3), 60.0) is False


def test_digital_silence_past_the_timeout_declares_the_stream_dead():
    """A jack switch leaves the WASAPI endpoint valid and returning zeros forever,
    so nothing raises and the retry loop must be triggered by silence instead."""
    watchdog = SilenceWatchdog(timeout_seconds=10.0)
    watchdog.reset(0.0)

    assert watchdog.observe(np.zeros(240, dtype=np.float32), 9.9) is False
    assert watchdog.observe(np.zeros(240, dtype=np.float32), 10.1) is True


def test_returning_audio_clears_a_pending_silence():
    watchdog = SilenceWatchdog(timeout_seconds=10.0)
    watchdog.reset(0.0)
    watchdog.observe(np.zeros(240, dtype=np.float32), 9.0)

    watchdog.observe(_noise(0.2), 9.5)

    assert watchdog.observe(np.zeros(240, dtype=np.float32), 18.0) is False


def test_a_disabled_watchdog_never_fires():
    watchdog = SilenceWatchdog(timeout_seconds=0)
    watchdog.reset(0.0)

    assert watchdog.observe(np.zeros(240, dtype=np.float32), 10_000.0) is False
