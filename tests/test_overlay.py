import asyncio

from meeting_agent.events import EventBus, SuggestionBatchEvent, SuggestionEvent
from meeting_agent.overlay import PLACEHOLDER, SuggestionOverlay, format_batch, overlay_bridge


def test_batch_renders_every_current_suggestion():
    batch = SuggestionBatchEvent(
        revision=4,
        suggestions=[SuggestionEvent("RISK", "Rollback plan is missing"), SuggestionEvent("QUESTION", "Who owns it?")],
    )

    rendered = format_batch(batch)

    assert "Rollback plan is missing" in rendered
    assert "Who owns it?" in rendered


def test_empty_batch_replaces_stale_advice_with_placeholder():
    assert format_batch(SuggestionBatchEvent(revision=2, suggestions=[])) == PLACEHOLDER


def test_overlay_drops_oldest_message_when_saturated():
    overlay = SuggestionOverlay()
    overlay.messages.maxsize = 2
    for index in range(5):
        overlay.push(SuggestionEvent("COMMENT", f"suggestion {index}"))

    assert overlay.messages.qsize() == 2
    assert "suggestion 4" in list(overlay.messages.queue)[-1]


async def test_overlay_bridge_forwards_batches_and_stops_cleanly(monkeypatch):
    monkeypatch.setattr(SuggestionOverlay, "start", lambda self: None)
    overlay = SuggestionOverlay()
    bus = EventBus()
    stop = asyncio.Event()
    task = asyncio.create_task(overlay_bridge(overlay, bus, stop))
    await asyncio.sleep(0)
    bus.publish(SuggestionBatchEvent(revision=1, suggestions=[SuggestionEvent("ACTION", "Send the notes")]))
    bus.publish(SuggestionEvent("RISK", "Standalone risk"))
    await asyncio.sleep(0.1)
    stop.set()
    await task

    messages = list(overlay.messages.queue)
    assert any("Send the notes" in str(message) for message in messages)
    assert any("Standalone risk" in str(message) for message in messages)
    assert messages[-1] is None
