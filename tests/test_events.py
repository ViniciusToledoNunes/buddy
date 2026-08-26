import asyncio

from meeting_agent.events import EventBus, TranscriptEvent


def test_event_bus_drops_oldest_for_slow_consumer():
    bus = EventBus()
    queue = bus.subscribe(maxsize=1)
    bus.publish(TranscriptEvent("ME", "first", True, "1"))
    bus.publish(TranscriptEvent("ME", "second", True, "2"))
    event = queue.get_nowait()
    assert event.text == "second"
