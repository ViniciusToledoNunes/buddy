import os
import subprocess
import sys

from meeting_agent.capture.macos import end_helper, read_frame


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
