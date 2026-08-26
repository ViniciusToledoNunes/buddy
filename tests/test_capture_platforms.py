from meeting_agent.capture import linux
from meeting_agent.config import AudioConfig


def test_pipewire_system_capture_command(monkeypatch):
    monkeypatch.setattr(linux.shutil, "which", lambda name: "/usr/bin/pw-record" if name == "pw-record" else None)
    command = linux.pipewire_command("REMOTE", AudioConfig(sample_rate=24000, chunk_ms=100))
    assert command[0] == "/usr/bin/pw-record"
    assert "--rate=24000" in command
    assert '--properties={"stream.capture.sink":true}' in command
    assert command[-1] == "-"
