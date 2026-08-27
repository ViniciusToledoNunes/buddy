import asyncio
import io

from rich.console import Console

from meeting_agent.config import Settings
from meeting_agent.events import (
    EventBus,
    StatusEvent,
    SuggestionBatchEvent,
    SuggestionEvent,
    TranscriptEvent,
)
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


def test_render_shows_transcript_partials_and_status():
    ui = LiveUI(Settings(), EventBus(), "meetings/now")
    ui.apply_event(TranscriptEvent("ME", "final line", True, "u1", latency_seconds=0.42))
    ui.apply_event(TranscriptEvent("REMOTE", "partial line", False, "u2"))
    ui.apply_event(StatusEvent("llm", "connected", "openai"))
    ui.apply_event(SuggestionBatchEvent(revision=1, suggestions=[SuggestionEvent("RISK", "Check rollback")]))

    rendered = _plain(ui)

    assert "final line" in rendered
    assert "partial line" in rendered
    assert "Check rollback" in rendered
    assert "llm: connected" in rendered
    assert ui.latency == 0.42


def test_render_waits_for_speech_and_prompts_for_suggestions():
    rendered = _plain(LiveUI(Settings(), EventBus(), "meetings/now"))

    assert "Waiting for speech" in rendered
    assert "Ctrl+Alt+Space" in rendered


def test_final_event_clears_its_partial():
    ui = LiveUI(Settings(), EventBus(), "meetings/now")
    ui.apply_event(TranscriptEvent("REMOTE", "half", False, "u1"))
    ui.apply_event(TranscriptEvent("REMOTE", "whole", True, "u1"))

    assert ui.partials == {}
    assert ui.lines[-1][2] == "whole"


async def test_run_consumes_events_until_stopped():
    bus = EventBus()
    ui = LiveUI(Settings.model_validate({"ui": {"refresh_hz": 30}}), bus, "meetings/now")
    stop = asyncio.Event()
    task = asyncio.create_task(ui.run(stop))
    await asyncio.sleep(0)
    bus.publish(TranscriptEvent("ME", "streamed", True, "u9"))
    await asyncio.sleep(0.1)
    stop.set()
    await task

    assert any(line[2] == "streamed" for line in ui.lines)


def _plain(ui: LiveUI) -> str:
    console = Console(width=100, record=True, file=io.StringIO())
    console.print(ui._render())
    return console.export_text()


def test_render_warns_when_global_hotkeys_could_not_register():
    ui = LiveUI(Settings(), EventBus(), "meetings/now")
    ui.apply_event(StatusEvent("hotkeys", "warning", "this platform is not supported"))

    console = Console(file=io.StringIO(), width=200)
    console.print(ui._render())
    output = console.file.getvalue()

    assert "hotkeys: warning" in output
