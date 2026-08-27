import json
import os
from datetime import datetime, timezone

import psutil

from meeting_agent import session as session_module
from meeting_agent.session import _hotkey, _state_process_alive, read_state, request_stop


def test_hotkey_translation_uses_pynput_syntax():
    assert _hotkey("ctrl+alt+m") == "<ctrl>+<alt>+m"
    assert _hotkey("ctrl+alt+space") == "<ctrl>+<alt>+<space>"


def _state(pid: int, started: str) -> dict[str, object]:
    return {"pid": pid, "started": started, "meeting_dir": "meetings/x", "recording": True}


def _own_start_time() -> str:
    return datetime.fromtimestamp(psutil.Process(os.getpid()).create_time(), timezone.utc).isoformat()


def test_live_state_is_recognised_and_a_dead_pid_is_not():
    assert _state_process_alive(_state(os.getpid(), _own_start_time())) is True
    assert _state_process_alive(_state(999_999_999, "2026-08-26T12:00:00+00:00")) is False
    assert _state_process_alive({"pid": "not-a-pid"}) is False


def test_read_state_tolerates_missing_and_corrupt_files(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "STATE_FILE", tmp_path / "state.json")
    assert read_state() is None

    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    assert read_state() is None

    (tmp_path / "state.json").write_text(json.dumps({"pid": 1}), encoding="utf-8")
    assert read_state() == {"pid": 1}


def test_request_stop_only_signals_a_live_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(session_module, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(session_module, "STOP_FILE", tmp_path / "stop.flag")

    assert request_stop() is False

    (tmp_path / "state.json").write_text(json.dumps(_state(999_999_999, "2026-08-26T12:00:00+00:00")), encoding="utf-8")
    assert request_stop() is False
    assert not (tmp_path / "stop.flag").exists()

    (tmp_path / "state.json").write_text(json.dumps(_state(os.getpid(), _own_start_time())), encoding="utf-8")
    assert request_stop() is True
    assert (tmp_path / "stop.flag").exists()
