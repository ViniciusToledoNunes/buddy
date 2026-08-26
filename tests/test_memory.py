import json

from meeting_agent.memory import MeetingMemoryIndex


def _memory(meetings, meeting_id, *, memory, topics, transcript="sensitive raw transcript"):
    directory = meetings / meeting_id
    directory.mkdir(parents=True)
    (directory / "copilot.json").write_text(
        json.dumps(
            {
                "memory": memory,
                "topics": topics,
                "decisions": [],
                "action_items": [],
                "open_questions": [],
            }
        ),
        encoding="utf-8",
    )
    (directory / "transcript.jsonl").write_text(transcript, encoding="utf-8")


def test_related_memory_uses_structured_context_and_ranks_relevant_meeting(tmp_path):
    meetings = tmp_path / "meetings"
    _memory(
        meetings,
        "2026-08-20_billing",
        memory="Invoice migration needs reconciliation and a rollback plan.",
        topics=["billing migration", "invoice reconciliation", "rollback"],
    )
    _memory(
        meetings,
        "2026-08-19_hiring",
        memory="The design team will interview two candidates.",
        topics=["hiring", "design"],
    )

    results = MeetingMemoryIndex(meetings).find_related("production invoice migration rollback", limit=2)

    assert results[0]["meeting_id"] == "2026-08-20_billing"
    assert results[0]["score"] > 0
    assert "sensitive raw transcript" not in json.dumps(results)


def test_related_memory_excludes_current_meeting_and_empty_noise(tmp_path):
    meetings = tmp_path / "meetings"
    _memory(meetings, "current", memory="Deployment discussion", topics=["deployment"])
    index = MeetingMemoryIndex(meetings)

    assert index.find_related("the and or", limit=3) == []
    assert index.find_related("deployment", exclude_meeting_id="current") == []
