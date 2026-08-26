import numpy as np

from meeting_agent.asr.local import resample_linear


def test_resample_24k_to_16k():
    source = np.zeros(2400, dtype=np.float32)
    output = resample_linear(source, 24000, 16000)
    assert output.dtype == np.float32
    assert len(output) == 1600
