from meeting_agent.capture import linux
from meeting_agent.config import AudioConfig


def test_pipewire_system_capture_command(monkeypatch):
    monkeypatch.setattr(linux.shutil, "which", lambda name: "/usr/bin/pw-record" if name == "pw-record" else None)
    command = linux.pipewire_command("REMOTE", AudioConfig(sample_rate=24000, chunk_ms=100))
    assert command[0] == "/usr/bin/pw-record"
    assert "--rate=24000" in command
    assert '--properties={"stream.capture.sink":true}' in command
    assert command[-1] == "-"


def test_pipewire_command_only_uses_options_pw_cat_accepts(monkeypatch):
    # pw-record is a symlink to pw-cat, which has no --raw: it writes raw PCM to "-"
    # anyway, and rejects the unknown option instead of capturing.
    monkeypatch.setattr(linux.shutil, "which", lambda name: "/usr/bin/pw-record" if name == "pw-record" else None)
    for speaker in ("ME", "REMOTE"):
        command = linux.pipewire_command(speaker, AudioConfig())
        assert "--raw" not in command
        assert "--record" in command  # the pw-cat fallback needs the explicit mode


def test_pipewire_microphone_capture_does_not_read_the_sink(monkeypatch):
    monkeypatch.setattr(linux.shutil, "which", lambda name: "/usr/bin/pw-record" if name == "pw-record" else None)
    command = linux.pipewire_command("ME", AudioConfig(microphone_device="Mic"))
    assert '--properties={"stream.capture.sink":true}' not in command
    assert "--target=Mic" in command


def test_capture_probe_reports_a_rejected_command(monkeypatch):
    monkeypatch.setattr(linux.shutil, "which", lambda name: "/bin/false")
    monkeypatch.setattr(
        linux, "pipewire_command",
        lambda speaker, config: ["/bin/sh", "-c", "echo 'unrecognized option' >&2; exit 1"],
    )
    working, detail = linux.capture_probe("REMOTE", AudioConfig(), seconds=2.0)
    assert working is False
    assert "unrecognized option" in detail


def test_capture_probe_reports_a_stream_that_produces_audio(monkeypatch):
    monkeypatch.setattr(
        linux, "pipewire_command",
        lambda speaker, config: ["/bin/sh", "-c", "head -c 4096 /dev/zero; sleep 5"],
    )
    working, detail = linux.capture_probe("ME", AudioConfig(), seconds=0.5)
    assert working is True
    assert "4096 bytes" in detail


def test_probe_fails_when_the_capture_command_produces_nothing(monkeypatch):
    monkeypatch.setattr(linux.shutil, "which", lambda name: "/usr/bin/pw-record")
    monkeypatch.setattr(linux, "_pipewire_nodes", lambda: [])
    monkeypatch.setattr(linux, "capture_probe", lambda speaker, config: (False, "captured no audio data"))
    states = {check["name"]: check["state"] for check in linux.probe(AudioConfig())}
    assert set(states.values()) == {"failed"}
