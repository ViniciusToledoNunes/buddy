import asyncio
import os
import stat
import subprocess
import sys
import threading
import time

import numpy as np

from meeting_agent.capture.macos import MacOSAudioCapture, end_helper, read_frame, retry_delay, sanitize
from meeting_agent.config import AudioConfig


def test_a_silent_helper_times_out_instead_of_blocking_forever():
    read_end, write_end = os.pipe()
    try:
        with os.fdopen(read_end, "rb", buffering=0) as stream:
            assert read_frame(stream, 8, timeout=0.2) is None
    finally:
        os.close(write_end)


def test_a_frame_split_across_writes_is_reassembled():
    read_end, write_end = os.pipe()
    os.write(write_end, b"abcd")
    os.write(write_end, b"efgh")
    with os.fdopen(read_end, "rb", buffering=0) as stream:
        assert read_frame(stream, 8, timeout=1) == b"abcdefgh"
    os.close(write_end)


def test_the_end_of_the_stream_gives_a_short_frame():
    read_end, write_end = os.pipe()
    os.write(write_end, b"abc")
    os.close(write_end)
    with os.fdopen(read_end, "rb", buffering=0) as stream:
        assert read_frame(stream, 8, timeout=1) == b"abc"


def test_a_helper_that_ignores_terminate_is_killed():
    stubborn = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
    process = subprocess.Popen([sys.executable, "-c", stubborn], stdout=subprocess.PIPE)
    assert process.stdout.readline().strip() == b"ready"

    end_helper(process)

    assert process.poll() is not None


def test_retry_backs_off_up_to_ten_seconds():
    assert [retry_delay(n) for n in range(1, 7)] == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]


def test_corrupted_frames_are_silenced():
    clean = np.array([0.1, -0.2], dtype=np.float32)
    samples, damaged = sanitize(clean)
    assert not damaged
    assert samples is clean
    for broken in ([np.nan, 0.1], [np.inf, 0.0], [1e20, 0.0]):
        samples, damaged = sanitize(np.array(broken, dtype=np.float32))
        assert damaged
        assert not samples.any()


def fake_helper(tmp_path, body: str) -> str:
    path = tmp_path / "meeting-audio-macos"
    path.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class Bus:
    def publish(self, event) -> None:
        pass


def capture(speaker: str, config: AudioConfig) -> tuple[MacOSAudioCapture, threading.Event]:
    stop = threading.Event()
    loop = asyncio.new_event_loop()
    return MacOSAudioCapture(speaker, config, asyncio.Queue(maxsize=100), stop, Bus(), loop), stop


def test_a_silent_system_helper_is_restarted(tmp_path, monkeypatch):
    starts = tmp_path / "starts"
    monkeypatch.setenv("BUDDY_MACOS_HELPER", fake_helper(tmp_path, f"open({str(starts)!r}, 'a').write('x')\ntime.sleep(60)"))
    stream, stop = capture("REMOTE", AudioConfig(helper_stall_seconds=0.3))
    stream.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(starts.read_text() if starts.exists() else "") < 2:
        time.sleep(0.05)
    stop.set()
    stream.join()
    assert len(starts.read_text()) >= 2


def test_join_ends_a_helper_blocked_on_read(tmp_path, monkeypatch):
    pid_file = tmp_path / "pid"
    body = f"import os\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)"
    monkeypatch.setenv("BUDDY_MACOS_HELPER", fake_helper(tmp_path, body))
    stream, stop = capture("REMOTE", AudioConfig(helper_stall_seconds=0))
    stream.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not pid_file.exists():
        time.sleep(0.05)
    stop.set()
    stream.join()
    pid = int(pid_file.read_text())
    time.sleep(0.2)
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive
    assert not stream.thread.is_alive()
