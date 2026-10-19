import asyncio
import json
import os

from meeting_agent.autobrain import AutoBrain, BrainRunner
from meeting_agent.copilot import LLMProvider
from meeting_agent.listener import ListenDaemon
from meeting_agent.live import LiveView
from test_listener import Clock, FakeStream, _events, _said
from meeting_agent.config import Settings


class FakeProvider(LLMProvider):
    """Answers like Claude Code would, and records what it was asked. Never runs anything."""

    def __init__(self, replies=None, fail=False, available=True):
        super().__init__()
        self.prompts = []
        self.replies = list(replies or [])
        self.fail = fail
        self._available = available

    def available(self):
        return self._available

    def system_prompt(self):
        return "copilot"

    async def complete(self, system, prompt):
        self.prompts.append((system, prompt))
        if self.fail:
            raise RuntimeError("boom")
        if system != "copilot":  # the report
            return "# Summary\n\nWe agreed to ship on Friday."
        if self.replies:
            return self.replies.pop(0)
        return json.dumps({"suggestions": [{"kind": "COMMENT", "text": "Say we ship on Friday."}]})


def _daemon(tmp_path, provider, clock=None, **listen):
    listen.setdefault("sounds", False)
    settings = Settings.model_validate({"meetings_dir": str(tmp_path / "meetings"), "listen": listen})
    daemon = ListenDaemon(
        settings, tmp_path / "runtime", stream_factory=lambda s: FakeStream(s, []), clock=clock or Clock(),
        brain_factory=lambda: AutoBrain(settings, provider_factory=lambda: provider),
    )
    daemon.chime = lambda kind: None
    daemon.claim()
    return daemon


def _feed(daemon):
    path = daemon.runtime_dir / "live.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


async def _settle(daemon):
    if daemon.brain is not None and daemon.brain._task is not None:
        await daemon.brain._task


async def test_with_no_session_watching_buddy_suggests_by_itself(tmp_path):
    provider = FakeProvider()
    daemon = _daemon(tmp_path, provider, echo_window_seconds=0)
    await daemon.start_meeting("test")

    await daemon.on_final(_said("REMOTE", "When can we ship the vendor page?"))  # a question sends the batch
    await _settle(daemon)

    suggest = [r for r in _feed(daemon) if r["type"] == "SUGGEST"]
    assert suggest[-1]["items"] == ["1. Say we ship on Friday."]
    system, prompt = provider.prompts[-1]
    assert system == "copilot" and "REMOTE: When can we ship the vendor page?" in prompt
    assert 'sent early because of a question: "When can we ship the vendor page?"' in prompt
    assert any("built-in brain is suggesting" in e.get("text", "") for e in _events(daemon))


async def test_a_watching_session_keeps_the_job(tmp_path):
    provider = FakeProvider()
    daemon = _daemon(tmp_path, provider, echo_window_seconds=0)
    # This test process stands in for a live Claude Code session following Buddy.
    from meeting_agent.listener import _process_started, _write_json

    _write_json(daemon.runtime_dir / "brain.json",
                {"session_id": "abc", "pid": os.getpid(), "started": _process_started(os.getpid())})
    await daemon.start_meeting("test")

    await daemon.on_final(_said("REMOTE", "When can we ship the vendor page?"))

    assert daemon.brain is None and provider.prompts == []


async def test_suggestions_are_numbered_across_the_meeting(tmp_path):
    reply = json.dumps({"suggestions": [{"text": "A"}, {"text": "B"}]})
    provider = FakeProvider(replies=[reply, reply])
    daemon = _daemon(tmp_path, provider, echo_window_seconds=0)
    await daemon.start_meeting("test")

    await daemon.on_final(_said("REMOTE", "What do we do about the brand field?"))
    await _settle(daemon)
    daemon.clock.now += 20
    await daemon.on_final(_said("REMOTE", "And who owns the rollback plan?"))
    await _settle(daemon)

    assert [r["items"] for r in _feed(daemon) if r["type"] == "SUGGEST"] == [["1. A", "2. B"], ["3. A", "4. B"]]


async def test_the_window_shows_the_built_in_brains_suggestions(tmp_path):
    daemon = _daemon(tmp_path, FakeProvider(), echo_window_seconds=0)
    view = LiveView(daemon.runtime_dir, home=tmp_path)
    await daemon.start_meeting("test")
    view.poll()

    await daemon.on_final(_said("REMOTE", "When can we ship the vendor page?"))
    await _settle(daemon)
    view.poll()

    assert view.status()["suggestions"] == ["1. Say we ship on Friday."]


async def test_the_built_in_brain_writes_the_summary(tmp_path):
    daemon = _daemon(tmp_path, FakeProvider(), echo_window_seconds=0)
    await daemon.start_meeting("test")
    storage = daemon.storage
    await daemon.on_final(_said("REMOTE", "When can we ship the vendor page?"))

    await daemon.stop_meeting("test")
    await daemon._summary_task

    assert storage.summary_md.read_text(encoding="utf-8").startswith("# Summary\n\nWe agreed to ship on Friday.")
    assert storage.metadata["brain"] == "built-in"


async def test_a_failed_call_never_stops_the_listening(tmp_path):
    daemon = _daemon(tmp_path, FakeProvider(fail=True), echo_window_seconds=0)
    await daemon.start_meeting("test")

    await daemon.on_final(_said("REMOTE", "When can we ship the vendor page?"))
    await _settle(daemon)

    assert daemon.storage is not None
    assert any("Built-in brain failed: RuntimeError: boom" in e.get("text", "") for e in _events(daemon))


async def test_an_unavailable_brain_is_said_once(tmp_path):
    daemon = _daemon(tmp_path, FakeProvider(available=False), echo_window_seconds=0, question_min_gap_seconds=0)
    await daemon.start_meeting("test")

    await daemon.on_final(_said("REMOTE", "When can we ship the vendor page?"))
    await daemon.on_final(_said("REMOTE", "And who owns the rollback plan?"))

    notices = [e for e in _events(daemon) if "built-in brain is unavailable" in e.get("text", "")]
    assert len(notices) == 1


async def test_it_can_be_turned_off(tmp_path):
    provider = FakeProvider()
    daemon = _daemon(tmp_path, provider, echo_window_seconds=0, auto_brain=False)
    await daemon.start_meeting("test")

    await daemon.on_final(_said("REMOTE", "When can we ship the vendor page?"))

    assert daemon.brain is None and provider.prompts == []


async def test_a_batch_that_arrives_mid_call_goes_out_next_with_its_question():
    gate = asyncio.Event()

    class Slow(FakeProvider):
        async def complete(self, system, prompt):
            self.prompts.append((system, prompt))
            if len(self.prompts) == 1:
                await gate.wait()
            return json.dumps({"suggestions": []})

    provider = Slow()
    runner = BrainRunner(AutoBrain(None, provider_factory=lambda: provider), lambda s: None, lambda t: None)
    runner.submit(["REMOTE: first"])
    await asyncio.sleep(0)
    runner.submit(["REMOTE: second"], {"trigger": "question", "question": "Who owns it?"})
    runner.submit(["REMOTE: third"])
    gate.set()
    await runner._task

    assert len(provider.prompts) == 2  # never more than one call in flight
    assert "REMOTE: second\nREMOTE: third" in provider.prompts[1][1]
    assert 'question: "Who owns it?"' in provider.prompts[1][1]
