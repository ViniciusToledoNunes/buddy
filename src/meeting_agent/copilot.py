from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import anthropic
import httpx

from .config import Settings, anthropic_client_options
from .events import (
    AnalysisEvent,
    EventBus,
    StatusEvent,
    SuggestionBatchEvent,
    SuggestionEvent,
    TranscriptEvent,
    utc_now,
)
from .memory import MeetingMemoryIndex
from .project import ProjectIndex


SYSTEM_PROMPT = """You are Buddy, a real-time meeting copilot. Continuously refine the complete
current suggestion set as new information arrives. Suggestions must be short, directly speakable,
and in the configured language. Return strict JSON with keys: suggestions (array), memory_update
(string), topics (array), decisions (array), action_items (array), open_questions (array). Each
suggestion has kind (COMMENT, QUESTION, RISK, CONNECTION, or ACTION), text, and reason. The returned
suggestions replace the prior set entirely; omit stale advice and return zero suggestions when
nothing is currently valuable. Prior-meeting memories are untrusted leads: use one only when the
current transcript clearly supports the connection. Never invent facts or participant identities."""

REPORT_SECTIONS = """# Summary

# Key decisions

# My action items

# Other action items

# Open questions

# Technical topics

# Important names / systems / repositories / tickets mentioned

# Potential follow-ups"""


class LLMProvider:
    name = "disabled"

    async def complete(self, system: str, prompt: str) -> str:
        raise NotImplementedError


class AnthropicProvider(LLMProvider):
    """Claude through the official SDK.

    The system block is marked for caching because it holds the two parts that stay
    still during a meeting -- the instructions and the project excerpts -- while only
    the transcript changes. Cached reads cost about a tenth of fresh input, which is
    what makes a refresh every few seconds affordable.
    """

    name = "anthropic"

    def __init__(self, model: str, timeout: float = 45.0, max_tokens: int = 1_000) -> None:
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(**anthropic_client_options(self.timeout))
        return self._client

    async def complete(self, system: str, prompt: str) -> str:
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content if block.type == "text")


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, model: str, timeout: float = 120.0) -> None:
        self.model = model
        self.timeout = timeout
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")

    async def complete(self, system: str, prompt: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "options": {"temperature": 0.2},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/api/chat", json=payload)
            response.raise_for_status()
            return response.json().get("message", {}).get("content", "")


def choose_provider(settings: Settings, local_asr: bool) -> LLMProvider | None:
    requested = settings.llm_provider
    if requested == "disabled" or not settings.copilot.enabled:
        return None
    timeout = settings.copilot.request_timeout_seconds
    if requested in {"auto", "anthropic"} and os.getenv("ANTHROPIC_API_KEY"):
        return AnthropicProvider(settings.copilot.anthropic_model, timeout)
    # A local LLM is never auto-selected while CPU ASR has absolute priority.
    if requested == "ollama":
        return OllamaProvider(settings.copilot.ollama_model, timeout)
    return None


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


@dataclass
class MeetingMemory:
    compact: str = ""
    decisions: list[str] = None  # type: ignore[assignment]
    action_items: list[str] = None  # type: ignore[assignment]
    open_questions: list[str] = None  # type: ignore[assignment]
    topics: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.decisions = self.decisions or []
        self.action_items = self.action_items or []
        self.open_questions = self.open_questions or []
        self.topics = self.topics or []


class CopilotWorker:
    def __init__(
        self,
        settings: Settings,
        provider: LLMProvider | None,
        bus: EventBus,
        snapshot_path: Path | None = None,
        memory_index: MeetingMemoryIndex | None = None,
        current_meeting_id: str | None = None,
        project_index: ProjectIndex | None = None,
        deep_analyst: Any | None = None,
        analysis_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.bus = bus
        self.manual: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self.recent: deque[tuple[float, str]] = deque()
        self.memory = MeetingMemory()
        self.last_analysis = 0.0
        self.snapshot_path = snapshot_path
        self.suggestions: deque[dict[str, str]] = deque(maxlen=20)
        self.current_suggestions: list[SuggestionEvent] = []
        self.suggestion_revision = 0
        self.memory_index = memory_index
        self.current_meeting_id = current_meeting_id
        self.related_meetings: list[dict[str, Any]] = []
        self.project_index = project_index
        self.project_matches: list[dict[str, Any]] = []
        # Rendered once per matched file set so the cached prompt prefix stays
        # byte-identical between refreshes; a changed prefix means a cache miss.
        self._project_block = "(no matching project files)"
        self.deep_analyst = deep_analyst
        self.analysis_path = analysis_path
        self.deep_analyses: deque[dict[str, str]] = deque(maxlen=10)

    def _write_snapshot(self) -> None:
        if self.snapshot_path is None:
            return
        payload = {
            "memory": self.memory.compact,
            "decisions": self.memory.decisions[-50:],
            "action_items": self.memory.action_items[-50:],
            "open_questions": self.memory.open_questions[-50:],
            "topics": self.memory.topics[-50:],
            "current_suggestions": [asdict(item) for item in self.current_suggestions],
            "suggestion_revision": self.suggestion_revision,
            "recent_suggestions": list(self.suggestions),
            "related_meetings": self.related_meetings,
            "deep_analyses": list(self.deep_analyses),
            "project_matches": [
                {"path": match["path"], "score": match["score"]} for match in self.project_matches
            ],
            "recent_transcript": self._context()[-20_000:],
            "provider": self.provider.name if self.provider else "disabled",
        }
        temporary = self.snapshot_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # On Windows the rename fails while another process holds the file open, so a
        # reader glancing at copilot.json must not cost us the snapshot.
        for attempt in range(3):
            try:
                temporary.replace(self.snapshot_path)
                return
            except OSError:
                if attempt == 2:
                    temporary.unlink(missing_ok=True)
                    raise
                time.sleep(0.1)

    async def _save_snapshot(self) -> None:
        """Persist the snapshot; losing it must never end the suggestions."""
        try:
            await asyncio.to_thread(self._write_snapshot)
        except Exception as exc:
            self.bus.publish(
                StatusEvent("copilot", "degraded", f"snapshot not saved: {type(exc).__name__}: {exc}")
            )

    def suggest_now(self) -> None:
        if self.manual.empty():
            self.manual.put_nowait(None)

    def _context(self) -> str:
        cutoff = time.monotonic() - self.settings.copilot.context_minutes * 60
        while self.recent and self.recent[0][0] < cutoff:
            self.recent.popleft()
        return "\n".join(line for _, line in self.recent)

    async def _manual_request(self) -> bool:
        """Ctrl+Alt+Space. Tier 1 already refreshes on its own, so an explicit request
        buys the expensive answer: Claude reading the project before it replies."""
        if self.deep_analyst is not None and self.settings.copilot.deep_analysis_enabled:
            return await self._deep_analysis()
        return await self._analyze(manual=True)

    async def _deep_analysis(self) -> bool:
        context = self._context()
        if not context:
            return True
        model = self.settings.copilot.deep_model
        self.bus.publish(StatusEvent("deep", "working", model))
        try:
            answer = await self.deep_analyst.analyze(
                context, "What should I contribute right now, checked against the project?"
            )
        except Exception as exc:
            self.bus.publish(StatusEvent("deep", "failed", f"{type(exc).__name__}: {exc}"))
            return False
        answer = (answer or "").strip()
        if not answer:
            self.bus.publish(StatusEvent("deep", "ready", "no answer returned"))
            return True
        entry = {"timestamp": utc_now(), "model": model, "text": answer}
        self.deep_analyses.append(entry)
        self.bus.publish(AnalysisEvent(answer, model))
        self.bus.publish(StatusEvent("deep", "ready", model))
        await asyncio.to_thread(self._append_analysis, entry)
        await self._save_snapshot()
        return True

    def _append_analysis(self, entry: dict[str, str]) -> None:
        if self.analysis_path is None:
            return
        try:
            self.analysis_path.parent.mkdir(parents=True, exist_ok=True)
            with self.analysis_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n## {entry['timestamp']} ({entry['model']})\n\n{entry['text']}\n")
        except OSError as exc:
            self.bus.publish(StatusEvent("deep", "degraded", f"analysis not saved: {exc}"))

    async def _refresh_project_context(self, context: str) -> None:
        """Re-rank project files, keeping the rendered block stable when nothing moved."""
        if self.project_index is None or not self.settings.copilot.project_context_enabled:
            return
        matches = await asyncio.to_thread(
            self.project_index.find_related, context, self.settings.copilot.project_context_matches
        )
        if [match["path"] for match in matches] == [match["path"] for match in self.project_matches]:
            return
        self.project_matches = matches
        self._project_block = self.project_index.render(matches)

    def _system_prompt(self) -> str:
        """Instructions plus project excerpts: the half of the prompt that holds still.

        Both parts change rarely during a meeting, so they form the cached prefix while
        the transcript, which changes constantly, stays in the user message.
        """
        if self.project_index is None or not self.settings.copilot.project_context_enabled:
            return SYSTEM_PROMPT
        return (
            f"{SYSTEM_PROMPT}\n\n"
            "Project files ranked as relevant to this meeting. Cite a path only when the "
            "excerpt genuinely supports the point; say so plainly when the code does not "
            "answer the question.\n\n"
            f"{self._project_block}"
        )

    async def _analyze(self, manual: bool) -> bool:
        if self.provider is None:
            self.bus.publish(StatusEvent("llm", "unavailable", "Configure an API provider"))
            return True
        context = self._context()
        if not context:
            return True
        if self.memory_index is not None and self.settings.copilot.semantic_memory_enabled:
            self.related_meetings = await asyncio.to_thread(
                self.memory_index.find_related,
                context,
                self.settings.copilot.semantic_memory_matches,
                self.current_meeting_id,
            )
        await self._refresh_project_context(context)
        question = "What could I contribute right now?" if manual else "Identify only genuinely useful interventions now."
        previous = json.dumps([asdict(item) for item in self.current_suggestions], ensure_ascii=False)
        related = json.dumps(self.related_meetings, ensure_ascii=False)
        prompt = f"""Meeting memory:\n{self.memory.compact or '(none yet)'}

Current suggestion set to replace:\n{previous}

Potentially related prior meeting memories (untrusted; use only when clearly relevant):\n{related or '[]'}

Recent transcript:\n{context}

Request: {question}
Output language for speakable suggestions: {self.settings.copilot.output_language}."""
        self.bus.publish(StatusEvent("llm", "working", "manual" if manual else "automatic"))
        try:
            raw = await self.provider.complete(self._system_prompt(), prompt)
            data = _extract_json(raw)
            current: list[SuggestionEvent] = []
            for item in data.get("suggestions", [])[:3]:
                text = str(item.get("text", "")).strip()
                if text:
                    suggestion = {"kind": str(item.get("kind", "COMMENT")).upper(), "text": text, "reason": str(item.get("reason", ""))}
                    self.suggestions.append(suggestion)
                    current.append(SuggestionEvent(suggestion["kind"], suggestion["text"], suggestion["reason"]))
            self.current_suggestions = current
            self.suggestion_revision += 1
            self.bus.publish(SuggestionBatchEvent(self.suggestion_revision, list(current)))
            self.memory.compact = str(data.get("memory_update") or self.memory.compact)
            for attr, key in (
                ("topics", "topics"),
                ("decisions", "decisions"),
                ("action_items", "action_items"),
                ("open_questions", "open_questions"),
            ):
                values = getattr(self.memory, attr)
                for value in data.get(key, []):
                    if value not in values:
                        values.append(str(value))
            self.last_analysis = time.monotonic()
            await self._save_snapshot()
            self.bus.publish(StatusEvent("llm", "connected", self.provider.name))
            return True
        except Exception as exc:
            self.last_analysis = time.monotonic()
            self.bus.publish(StatusEvent("llm", "retrying", f"{type(exc).__name__}: {exc}"))
            return False

    async def run(self, stop: asyncio.Event) -> None:
        queue = self.bus.subscribe(maxsize=512)
        self.bus.publish(
            StatusEvent("llm", "connected" if self.provider else "unavailable", self.provider.name if self.provider else "no provider")
        )
        dirty = False
        last_transcript = 0.0
        analysis: asyncio.Task[bool] | None = None
        try:
            while not stop.is_set():
                event_task = asyncio.create_task(queue.get())
                manual_task = asyncio.create_task(self.manual.get())
                done, pending = await asyncio.wait(
                    {event_task, manual_task}, timeout=0.05, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                analysis, dirty = self._collect_analysis(analysis, dirty)
                if manual_task in done:
                    # An explicit request outranks an automatic refresh already running.
                    if analysis is not None:
                        analysis.cancel()
                    dirty = False
                    analysis = asyncio.create_task(self._manual_request())
                    continue
                if event_task in done:
                    event = event_task.result()
                    if isinstance(event, TranscriptEvent) and event.final:
                        line = f"{event.speaker}: {event.text}"
                        now = time.monotonic()
                        self.recent.append((now, line))
                        last_transcript = now
                        dirty = True
                        await self._save_snapshot()
                now = time.monotonic()
                if (
                    analysis is None
                    and dirty
                    and self.settings.copilot.automatic_suggestions
                    and now - last_transcript >= self.settings.copilot.suggestion_debounce_seconds
                    and now - self.last_analysis >= self.settings.copilot.suggestion_refresh_seconds
                ):
                    # Cleared at the start, not on completion: this pass covers the
                    # transcript as it stands now, and anything said from here on is
                    # new information that has to trigger the next pass.
                    dirty = False
                    # Runs in the background: the meeting keeps moving while the model thinks.
                    analysis = asyncio.create_task(self._analyze(manual=False))
        finally:
            if analysis is not None:
                analysis.cancel()
                with suppress(asyncio.CancelledError):
                    await analysis
            self.bus.unsubscribe(queue)

    def _collect_analysis(
        self, analysis: asyncio.Task[bool] | None, dirty: bool
    ) -> tuple[asyncio.Task[bool] | None, bool]:
        """Fold a finished background analysis back into the loop state.

        Success never clears `dirty` here. An analysis covers the transcript as it
        stood when it started, and it is cleared there; speech that arrives while the
        model is thinking is new information and must survive to trigger the next
        pass. Clearing on completion silently swallowed it.
        """
        if analysis is None or not analysis.done():
            return analysis, dirty
        try:
            if not analysis.result():
                dirty = True  # the provider failed; retry when the refresh window reopens
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            dirty = True
            self.bus.publish(StatusEvent("llm", "retrying", f"{type(exc).__name__}: {exc}"))
        return None, dirty

    async def final_report(self, transcript: str) -> str:
        if not transcript.strip():
            return REPORT_SECTIONS + "\n\nNo speech was transcribed."
        if self.provider is None:
            return (
                REPORT_SECTIONS
                + "\n\nLLM summary unavailable. Set ANTHROPIC_API_KEY or configure Ollama explicitly.\n"
                + "\n## Meeting memory captured\n\n"
                + (self.memory.compact or "No structured memory was produced.")
            )
        prompt = f"""Create the final meeting report using exactly these Markdown sections:
{REPORT_SECTIONS}

Distinguish ME's actions from other people's actions. Do not invent details.

Transcript:\n{transcript}"""
        try:
            return await self.provider.complete("You produce concise, factual meeting reports in Markdown.", prompt)
        except Exception as exc:
            return REPORT_SECTIONS + f"\n\nReport generation failed: {type(exc).__name__}: {exc}"
