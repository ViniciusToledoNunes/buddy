from meeting_agent.config import Settings
from meeting_agent.events import EventBus, SuggestionBatchEvent, SuggestionEvent
from meeting_agent.ui import LiveUI


def test_suggestion_batch_replaces_visible_suggestions():
    ui = LiveUI(Settings(), EventBus(), "meeting")
    old = SuggestionEvent("QUESTION", "Old question")
    new = SuggestionEvent("RISK", "Updated risk")

    ui.apply_event(SuggestionBatchEvent(revision=1, suggestions=[old]))
    ui.apply_event(SuggestionBatchEvent(revision=2, suggestions=[new]))

    assert list(ui.suggestions) == [new]


def test_empty_suggestion_batch_clears_stale_panel():
    ui = LiveUI(Settings(), EventBus(), "meeting")
    ui.apply_event(SuggestionBatchEvent(revision=1, suggestions=[SuggestionEvent("ACTION", "Do this")]))

    ui.apply_event(SuggestionBatchEvent(revision=2, suggestions=[]))

    assert list(ui.suggestions) == []
