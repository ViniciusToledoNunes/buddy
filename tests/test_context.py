import json
import os
from datetime import datetime, timezone

import psutil
import pytest

from meeting_agent.context import MeetingRepository


def make_meeting(tmp_path):
    meetings = tmp_path / "meetings"
    meeting = meetings / "2026-08-26_120000"
    meeting.mkdir(parents=True)
    (meeting / "metadata.json").write_text(json.dumps({"started_at": "2026-08-26T12:00:00+00:00", "recording": False}), encoding="utf-8")
    events = [
        {"speaker": "REMOTE", "text": "Should we validate the billing migration?", "timestamp": datetime.now(timezone.utc).isoformat(), "final": True},
        {"speaker": "ME", "text": "I will inspect the migration tests.", "timestamp": datetime.now(timezone.utc).isoformat(), "final": True},
    ]
    (meeting / "transcript.jsonl").write_text("\n".join(json.dumps(x) for x in events), encoding="utf-8")
    (meeting / "summary.md").write_text("# Summary\nBilling migration review", encoding="utf-8")
    return MeetingRepository(meetings, tmp_path / "runtime"), meeting


def test_repository_reads_and_searches_bounded_context(tmp_path):
    repo, meeting = make_meeting(tmp_path)
    assert repo.list_meetings(100)[0]["meeting_id"] == meeting.name
    assert len(repo.transcript("latest", speaker="ME")) == 1
    assert repo.search("billing")[0]["speaker"] == "REMOTE"
    assert repo.meeting("latest", include_transcript=False)["summary"].startswith("# Summary")


def test_repository_rejects_path_traversal(tmp_path):
    repo, _ = make_meeting(tmp_path)
    with pytest.raises(ValueError, match="Invalid meeting_id"):
        repo.meeting("../secrets")


def test_control_flags_require_active_session(tmp_path):
    repo, meeting = make_meeting(tmp_path)
    assert repo.request_suggestion()["accepted"] is False
    repo.runtime_dir.mkdir()
    process_started = datetime.fromtimestamp(psutil.Process(os.getpid()).create_time(), timezone.utc).isoformat()
    (repo.runtime_dir / "state.json").write_text(
        json.dumps({"pid": os.getpid(), "started": process_started, "meeting_dir": str(meeting), "recording": True}),
        encoding="utf-8",
    )
    assert repo.request_suggestion()["accepted"] is True
    assert (repo.runtime_dir / "suggest.flag").exists()


def test_stale_runtime_state_is_not_reported_active(tmp_path):
    repo, meeting = make_meeting(tmp_path)
    repo.runtime_dir.mkdir()
    (repo.runtime_dir / "state.json").write_text(
        json.dumps({"pid": 999_999_999, "started": "2026-08-26T12:00:00+00:00", "meeting_dir": str(meeting), "recording": True}),
        encoding="utf-8",
    )
    status = repo.status()
    assert status["active"] is False
    assert status["stale_state"] is True
