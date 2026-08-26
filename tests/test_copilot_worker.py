import asyncio
import json

from meeting_agent.config import Settings
from meeting_agent.copilot import CopilotWorker, LLMProvider
from meeting_agent.events import EventBus, SuggestionBatchEvent, TranscriptEvent


class FakeProvider(LLMProvider):
    name = "fake"

    async def complete(self, system: str, prompt: str) -> str:
        return json.dumps(
            {
                "suggestions": [{"kind": "QUESTION", "text": "Should we validate this in staging?", "reason": "risk"}],
                "memory_update": "A production change is under discussion.",
                "decisions": [],
                "action_items": [],
                "open_questions": ["Validation plan"],
            }
        )


async def test_manual_suggestion_runs_independently():
    bus = EventBus()
    output = bus.subscribe()
    worker = CopilotWorker(Settings(), FakeProvider(), bus)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)
    bus.publish(TranscriptEvent("REMOTE", "We may change production config.", True, "one"))
    await asyncio.sleep(0.05)
    worker.suggest_now()
    suggestion = None
    for _ in range(20):
        event = await asyncio.wait_for(output.get(), 1)
        if isinstance(event, SuggestionBatchEvent):
            suggestion = event.suggestions[0]
            break
    stop.set()
    await task
    assert suggestion is not None
    assert suggestion.kind == "QUESTION"


class SequencedProvider(LLMProvider):
    name = "sequence"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, system: str, prompt: str) -> str:
        self.calls.append(prompt)
        number = len(self.calls)
        suggestions = (
            [{"kind": "COMMENT", "text": f"Current suggestion {number}", "reason": "new context"}]
            if number < 3
            else []
        )
        return json.dumps(
            {
                "suggestions": suggestions,
                "memory_update": f"revision {number}",
                "topics": ["deployment"],
                "decisions": [],
                "action_items": [],
                "open_questions": [],
            }
        )


async def _next_batch(queue):
    for _ in range(40):
        event = await asyncio.wait_for(queue.get(), 1)
        if isinstance(event, SuggestionBatchEvent):
            return event
    raise AssertionError("suggestion batch not received")


async def test_automatic_suggestions_refresh_on_every_new_context():
    settings = Settings.model_validate(
        {
            "copilot": {
                "suggestion_refresh_seconds": 0.05,
                "suggestion_debounce_seconds": 0.01,
                "automatic_suggestions": True,
            }
        }
    )
    bus = EventBus()
    output = bus.subscribe()
    provider = SequencedProvider()
    worker = CopilotWorker(settings, provider, bus)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)

    # No trigger word or question mark: ordinary new information must still refresh Buddy.
    bus.publish(TranscriptEvent("REMOTE", "The rollout is scheduled for Tuesday.", True, "one"))
    first = await _next_batch(output)
    assert first.revision == 1
    assert first.suggestions[0].text == "Current suggestion 1"

    bus.publish(TranscriptEvent("REMOTE", "The staging window moved to Wednesday.", True, "two"))
    second = await _next_batch(output)
    assert second.revision == 2
    assert second.suggestions[0].text == "Current suggestion 2"

    bus.publish(TranscriptEvent("ME", "That resolves my concern.", True, "three"))
    third = await _next_batch(output)
    assert third.revision == 3
    assert third.suggestions == []
    assert worker.current_suggestions == []

    stop.set()
    await task
