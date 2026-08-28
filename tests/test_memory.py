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


def test_render_all_returns_every_recent_meeting(tmp_path):
    """Retrieval picked three by term overlap and got it wrong often enough to mislead;
    the whole set is small and lets the model decide what is relevant."""
    meetings = tmp_path / "meetings"
    for index in range(4):
        _memory(meetings, f"2026-08-2{index}_m", memory=f"memory {index}", topics=[f"topic{index}"])

    rendered = MeetingMemoryIndex(meetings).render_all()

    for index in range(4):
        assert f"memory {index}" in rendered
        assert f"topic{index}" in rendered
    assert "sensitive raw transcript" not in rendered


def test_render_all_excludes_the_current_meeting(tmp_path):
    meetings = tmp_path / "meetings"
    _memory(meetings, "current", memory="live one", topics=["now"])
    _memory(meetings, "older", memory="earlier one", topics=["before"])

    rendered = MeetingMemoryIndex(meetings).render_all(exclude_meeting_id="current")

    assert "earlier one" in rendered
    assert "live one" not in rendered


def test_render_all_with_no_history_is_explicit(tmp_path):
    assert "no prior meetings" in MeetingMemoryIndex(tmp_path / "none").render_all()
