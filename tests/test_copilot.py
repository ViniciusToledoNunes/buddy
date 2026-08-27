import json
import time

from meeting_agent.config import Settings
from meeting_agent.copilot import (
    AnthropicProvider,
    CopilotWorker,
    LLMProvider,
    OllamaProvider,
    OpenAIProvider,
    _extract_json,
    choose_provider,
)
from meeting_agent.events import EventBus, StatusEvent, SuggestionBatchEvent, TranscriptEvent
from meeting_agent.memory import MeetingMemoryIndex


def test_json_fence_parser():
    assert _extract_json('```json\n{"suggestions": []}\n```') == {"suggestions": []}
    assert _extract_json('noise before {"a": 1} noise after') == {"a": 1}


def test_provider_selection_prefers_explicit_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()

    assert choose_provider(settings, local_asr=True) is None

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert isinstance(choose_provider(settings, local_asr=True), AnthropicProvider)

    monkeypatch.setenv("OPENAI_API_KEY", "y")
    assert isinstance(choose_provider(settings, local_asr=True), OpenAIProvider)

    assert isinstance(choose_provider(Settings(llm_provider="ollama"), local_asr=True), OllamaProvider)
    assert choose_provider(Settings(llm_provider="disabled"), local_asr=True) is None
    assert choose_provider(Settings.model_validate({"copilot": {"enabled": False}}), local_asr=True) is None


class FailingProvider(LLMProvider):
    name = "failing"

    async def complete(self, system: str, prompt: str) -> str:
        raise RuntimeError("provider offline")


async def test_provider_failure_is_reported_and_never_crashes_the_worker():
    bus = EventBus()
    output = bus.subscribe()
    worker = CopilotWorker(Settings(), FailingProvider(), bus)
    worker.recent.append((time.monotonic(), "REMOTE: something happened"))

    assert await worker._analyze(manual=True) is False

    states = [event for event in _drain(output) if isinstance(event, StatusEvent)]
    assert any(event.state == "retrying" and "provider offline" in event.detail for event in states)


async def test_disabled_provider_publishes_status_instead_of_suggesting():
    bus = EventBus()
    output = bus.subscribe()
    worker = CopilotWorker(Settings(), None, bus)

    assert await worker._analyze(manual=False) is True

    assert any(
        isinstance(event, StatusEvent) and event.state == "unavailable" for event in _drain(output)
    )


def _drain(queue):
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


class RecordingProvider(LLMProvider):
    name = "recording"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {
                "suggestions": [{"kind": "CONNECTION", "text": "Reuse the rollback plan", "reason": "prior meeting"}],
                "memory_update": "rollback discussion",
                "topics": ["rollback"],
                "decisions": ["reuse the plan"],
                "action_items": [],
                "open_questions": [],
            }
        )


async def test_related_meetings_reach_the_prompt_and_snapshot(tmp_path):
    meetings = tmp_path / "meetings"
    prior = meetings / "2026-08-20_billing"
    prior.mkdir(parents=True)
    (prior / "copilot.json").write_text(
        json.dumps({"memory": "Invoice rollback plan agreed.", "topics": ["rollback", "invoice"]}),
        encoding="utf-8",
    )
    snapshot = tmp_path / "copilot.json"
    provider = RecordingProvider()
    worker = CopilotWorker(
        Settings(),
        provider,
        EventBus(),
        snapshot_path=snapshot,
        memory_index=MeetingMemoryIndex(meetings),
        current_meeting_id="current",
    )
    worker.recent.append((time.monotonic(), "REMOTE: we need an invoice rollback again"))

    assert await worker._analyze(manual=False) is True

    assert "Invoice rollback plan agreed." in provider.prompts[0]
    assert "untrusted" in provider.prompts[0].lower()
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["related_meetings"][0]["meeting_id"] == prior.name
    assert saved["current_suggestions"][0]["text"] == "Reuse the rollback plan"
    assert saved["suggestion_revision"] == 1


async def test_memory_accumulates_without_duplicates():
    worker = CopilotWorker(Settings(), RecordingProvider(), EventBus())
    worker.recent.append((time.monotonic(), "REMOTE: rollback"))

    await worker._analyze(manual=False)
    await worker._analyze(manual=False)

    assert worker.memory.decisions == ["reuse the plan"]
    assert worker.memory.topics == ["rollback"]
    assert worker.suggestion_revision == 2


async def test_context_window_drops_stale_lines():
    settings = Settings.model_validate({"copilot": {"context_minutes": 1}})
    worker = CopilotWorker(settings, None, EventBus())
    worker.recent.append((time.monotonic() - 3_600, "REMOTE: ancient line"))
    worker.recent.append((time.monotonic(), "REMOTE: fresh line"))

    context = worker._context()

    assert "ancient line" not in context
    assert "fresh line" in context


async def test_final_report_paths():
    worker = CopilotWorker(Settings(), None, EventBus())
    assert "No speech was transcribed." in await worker.final_report("   ")
    assert "LLM summary unavailable" in await worker.final_report("ME: hello")

    worker.provider = RecordingProvider()
    assert "suggestions" in await worker.final_report("ME: hello")

    worker.provider = FailingProvider()
    assert "Report generation failed" in await worker.final_report("ME: hello")


async def test_manual_request_is_deduplicated():
    worker = CopilotWorker(Settings(), None, EventBus())
    worker.suggest_now()
    worker.suggest_now()

    assert worker.manual.qsize() == 1
