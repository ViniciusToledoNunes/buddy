import asyncio
import json

from meeting_agent.config import Settings
from meeting_agent.copilot import CopilotWorker, LLMProvider
from meeting_agent.events import EventBus, SuggestionEvent, TranscriptEvent


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
        if isinstance(event, SuggestionEvent):
            suggestion = event
            break
    stop.set()
    await task
    assert suggestion is not None
    assert suggestion.kind == "QUESTION"
