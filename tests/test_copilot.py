import asyncio
import json
import time

from meeting_agent.config import Settings
from meeting_agent.copilot import (
    SYSTEM_PROMPT,
    AnthropicProvider,
    CopilotWorker,
    LLMProvider,
    OllamaProvider,
    _extract_json,
    choose_provider,
)
from meeting_agent.events import (
    AnalysisEvent,
    EventBus,
    StatusEvent,
    SuggestionBatchEvent,
    TranscriptEvent,
)
from meeting_agent.memory import MeetingMemoryIndex
from meeting_agent.project import ProjectIndex


def test_json_fence_parser():
    assert _extract_json('```json\n{"suggestions": []}\n```') == {"suggestions": []}
    assert _extract_json('noise before {"a": 1} noise after') == {"a": 1}


def test_provider_selection_prefers_explicit_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings()

    assert choose_provider(settings, local_asr=True) is None

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert isinstance(choose_provider(settings, local_asr=True), AnthropicProvider)

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


class SlowProvider(LLMProvider):
    name = "slow"

    def __init__(self, delay: float = 0.4) -> None:
        self.delay = delay
        self.started = 0
        self.finished = 0

    async def complete(self, system: str, prompt: str) -> str:
        self.started += 1
        await asyncio.sleep(self.delay)
        self.finished += 1
        return json.dumps({"suggestions": [], "memory_update": "slow", "topics": [], "decisions": [], "action_items": [], "open_questions": []})


def _continuous_settings(**overrides):
    copilot = {"suggestion_refresh_seconds": 0.05, "suggestion_debounce_seconds": 0.01}
    copilot.update(overrides)
    return Settings.model_validate({"copilot": copilot})


async def test_transcript_keeps_flowing_while_an_analysis_is_in_flight():
    """A blocking analysis stalls transcript ingestion, so the copilot's own context
    falls behind the meeting exactly when the conversation is most active."""
    bus = EventBus()
    provider = SlowProvider(delay=0.4)
    worker = CopilotWorker(_continuous_settings(), provider, bus)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)

    bus.publish(TranscriptEvent("REMOTE", "first line", True, "u1"))
    await asyncio.sleep(0.15)
    assert provider.started == 1 and provider.finished == 0  # analysis is in flight

    bus.publish(TranscriptEvent("REMOTE", "second line while thinking", True, "u2"))
    await asyncio.sleep(0.15)

    stop.set()
    await task
    assert "second line while thinking" in worker._context()


async def test_only_one_analysis_runs_at_a_time():
    bus = EventBus()
    provider = SlowProvider(delay=0.3)
    worker = CopilotWorker(_continuous_settings(), provider, bus)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)

    for index in range(5):
        bus.publish(TranscriptEvent("REMOTE", f"line {index}", True, f"u{index}"))
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.15)

    assert provider.started == 1

    stop.set()
    await task


async def test_snapshot_failure_reports_status_and_keeps_the_worker_alive(tmp_path, monkeypatch):
    """os.replace can fail transiently on Windows; that used to kill the copilot task
    silently and end suggestions for the rest of the meeting."""
    bus = EventBus()
    output = bus.subscribe()
    worker = CopilotWorker(Settings(), None, bus, snapshot_path=tmp_path / "copilot.json")
    monkeypatch.setattr(worker, "_write_snapshot", _explode)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)

    bus.publish(TranscriptEvent("REMOTE", "still recording", True, "u1"))
    await asyncio.sleep(0.15)

    assert not task.done()
    stop.set()
    await task

    events = _drain(output)
    assert any(isinstance(e, StatusEvent) and e.component == "copilot" and e.state == "degraded" for e in events)
    assert "still recording" in worker._context()


def _explode(*_args, **_kwargs):
    raise PermissionError("file is locked by another process")


def test_snapshot_write_retries_a_locked_destination(tmp_path, monkeypatch):
    snapshot = tmp_path / "copilot.json"
    worker = CopilotWorker(Settings(), None, EventBus(), snapshot_path=snapshot)
    attempts = {"count": 0}
    real_replace = type(snapshot).replace

    def flaky(self, target):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise PermissionError("locked")
        return real_replace(self, target)

    monkeypatch.setattr(type(snapshot), "replace", flaky)
    worker._write_snapshot()

    assert attempts["count"] == 2
    assert json.loads(snapshot.read_text(encoding="utf-8"))["provider"] == "disabled"
    assert not snapshot.with_suffix(".json.tmp").exists()


async def test_provider_timeout_comes_from_configuration():
    settings = Settings.model_validate({"copilot": {"request_timeout_seconds": 90}})
    assert settings.copilot.request_timeout_seconds == 90


async def test_speech_during_an_analysis_still_triggers_the_next_one():
    """The analysis covers the transcript as it stood when it started. Anything said
    while the model is thinking is new information, not already-handled context."""
    bus = EventBus()
    provider = SlowProvider(delay=0.3)
    worker = CopilotWorker(_continuous_settings(), provider, bus)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)

    bus.publish(TranscriptEvent("REMOTE", "opening point", True, "u1"))
    await asyncio.sleep(0.1)
    assert provider.started == 1 and provider.finished == 0

    bus.publish(TranscriptEvent("REMOTE", "said while the model was thinking", True, "u2"))
    await asyncio.sleep(0.6)

    stop.set()
    await task
    assert provider.started == 2


async def test_project_excerpts_reach_the_cached_prefix(tmp_path):
    """Project context belongs in the system block: it holds still while the transcript
    moves, which is what makes the cached prefix worth anything."""
    (tmp_path / "billing").mkdir()
    (tmp_path / "billing" / "rollback.py").write_text("def rollback_invoice(): pass\n", encoding="utf-8")
    provider = RecordingProvider()
    worker = CopilotWorker(
        Settings(), provider, EventBus(), project_index=ProjectIndex(tmp_path)
    )
    worker.recent.append((time.monotonic(), "REMOTE: can we rollback the invoice?"))

    await worker._analyze(manual=False)

    assert "billing/rollback.py" in worker._system_prompt()
    assert "rollback_invoice" in worker._system_prompt()
    assert worker.project_matches[0]["path"] == "billing/rollback.py"


async def test_the_cached_prefix_is_stable_while_the_same_files_match(tmp_path):
    (tmp_path / "app.py").write_text("def rollback(): pass\n", encoding="utf-8")
    worker = CopilotWorker(
        Settings(), RecordingProvider(), EventBus(), project_index=ProjectIndex(tmp_path)
    )
    worker.recent.append((time.monotonic(), "REMOTE: rollback"))

    await worker._analyze(manual=False)
    first = worker._system_prompt()
    worker.recent.append((time.monotonic(), "REMOTE: rollback again please"))
    await worker._analyze(manual=False)

    assert worker._system_prompt() == first


async def test_project_context_can_be_switched_off(tmp_path):
    (tmp_path / "app.py").write_text("def rollback(): pass\n", encoding="utf-8")
    settings = Settings.model_validate({"copilot": {"project_context_enabled": False}})
    worker = CopilotWorker(
        settings, RecordingProvider(), EventBus(), project_index=ProjectIndex(tmp_path)
    )
    worker.recent.append((time.monotonic(), "REMOTE: rollback"))

    await worker._analyze(manual=False)

    assert worker._system_prompt() == SYSTEM_PROMPT
    assert worker.project_matches == []


class StubAnalyst:
    def __init__(self, answer="storage.py:12 already appends every final event."):
        self.answer = answer
        self.calls = []

    async def analyze(self, transcript, question):
        self.calls.append((transcript, question))
        return self.answer


async def test_manual_request_runs_the_deep_analysis(tmp_path):
    """Ctrl+Alt+Space is the deep question now: tier 1 already refreshes on its own."""
    bus = EventBus()
    output = bus.subscribe()
    analyst = StubAnalyst()
    worker = CopilotWorker(
        Settings(), RecordingProvider(), bus, analysis_path=tmp_path / "analysis.md", deep_analyst=analyst
    )
    worker.recent.append((time.monotonic(), "REMOTE: do we already store every event?"))

    assert await worker._deep_analysis() is True

    assert analyst.calls and "store every event" in analyst.calls[0][0]
    events = _drain(output)
    assert any(isinstance(e, AnalysisEvent) and "storage.py:12" in e.text for e in events)
    assert "storage.py:12" in (tmp_path / "analysis.md").read_text(encoding="utf-8")
    assert worker.deep_analyses[-1]["text"].startswith("storage.py:12")


async def test_a_failing_deep_analysis_reports_and_does_not_raise(tmp_path):
    class Broken:
        async def analyze(self, transcript, question):
            raise RuntimeError("opus unreachable")

    bus = EventBus()
    output = bus.subscribe()
    worker = CopilotWorker(Settings(), RecordingProvider(), bus, deep_analyst=Broken())
    worker.recent.append((time.monotonic(), "REMOTE: anything"))

    assert await worker._deep_analysis() is False

    assert any(
        isinstance(e, StatusEvent) and e.component == "deep" and e.state == "failed" for e in _drain(output)
    )


async def test_manual_falls_back_to_tier_one_without_a_deep_analyst():
    bus = EventBus()
    provider = RecordingProvider()
    worker = CopilotWorker(Settings(), provider, bus)
    worker.recent.append((time.monotonic(), "REMOTE: anything"))

    await worker._manual_request()

    assert provider.prompts  # tier 1 ran instead


async def test_deep_analysis_can_be_switched_off(tmp_path):
    settings = Settings.model_validate({"copilot": {"deep_analysis_enabled": False}})
    provider = RecordingProvider()
    analyst = StubAnalyst()
    worker = CopilotWorker(settings, provider, EventBus(), deep_analyst=analyst)
    worker.recent.append((time.monotonic(), "REMOTE: anything"))

    await worker._manual_request()

    assert analyst.calls == []
    assert provider.prompts


class _Usage:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


def test_usage_accumulates_and_counts_calls():
    from meeting_agent.copilot import add_usage, usage_dict

    first = usage_dict(_Usage(input_tokens=100, output_tokens=20, cache_read_input_tokens=900))
    assert first["cache_creation_input_tokens"] == 0  # absent field, not a crash

    total = add_usage({}, first)
    total = add_usage(total, first)

    assert total["input_tokens"] == 200
    assert total["cache_read_input_tokens"] == 1800
    assert total["calls"] == 2


class MeteredProvider(LLMProvider):
    name = "metered"

    def __init__(self):
        super().__init__()
        self.prompts = []

    async def complete(self, system, prompt):
        self.prompts.append(prompt)
        self.last_usage = {
            "input_tokens": 1_000,
            "output_tokens": 200,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 3_000,
        }
        return json.dumps({"suggestions": [], "memory_update": "m", "topics": [], "decisions": [], "action_items": [], "open_questions": []})


async def test_tier_one_usage_reaches_the_snapshot(tmp_path):
    """Without this, there is no way to tell whether the cached prefix ever engaged."""
    snapshot = tmp_path / "copilot.json"
    worker = CopilotWorker(Settings(), MeteredProvider(), EventBus(), snapshot_path=snapshot)
    worker.recent.append((time.monotonic(), "REMOTE: anything"))

    await worker._analyze(manual=False)
    await worker._analyze(manual=False)

    saved = json.loads(snapshot.read_text(encoding="utf-8"))["usage"]
    assert saved["tier1"]["calls"] == 2
    assert saved["tier1"]["cache_read_input_tokens"] == 6_000


async def test_tier_two_usage_is_recorded_separately(tmp_path):
    class MeteredAnalyst:
        last_usage = {"input_tokens": 20_000, "output_tokens": 1_500, "calls": 5}

        async def analyze(self, transcript, question):
            return "answer citing storage.py:12"

    worker = CopilotWorker(Settings(), MeteredProvider(), EventBus(), deep_analyst=MeteredAnalyst())
    worker.recent.append((time.monotonic(), "REMOTE: anything"))

    await worker._deep_analysis()

    assert worker.usage["tier2"]["input_tokens"] == 20_000
    assert worker.usage["tier1"] == {}
