import json
import wave
import numpy as np

from meeting_agent.config import Settings
from meeting_agent.events import TranscriptEvent
from meeting_agent.audio import AudioFrame
from meeting_agent.storage import AudioArchiver, MeetingStorage


def test_incremental_storage(tmp_path):
    settings = Settings(meetings_dir=str(tmp_path))
    storage = MeetingStorage(settings, {"mode": "test"})
    storage.append(TranscriptEvent("REMOTE", "hello", True, "abc"))
    storage.finish("# Summary\n\nDone")
    line = json.loads(storage.transcript_jsonl.read_text(encoding="utf-8"))
    assert line["speaker"] == "REMOTE"
    assert "REMOTE: hello" in storage.transcript_txt.read_text(encoding="utf-8")
    assert storage.summary_md.exists()


def test_optional_audio_archive(tmp_path):
    archive = AudioArchiver(tmp_path, 24000)
    archive.write(AudioFrame("ME", np.zeros(2400, dtype=np.float32), 0.0, 0))
    archive.close()
    with wave.open(str(tmp_path / "audio_me.wav"), "rb") as handle:
        assert handle.getframerate() == 24000
        assert handle.getnframes() == 2400
