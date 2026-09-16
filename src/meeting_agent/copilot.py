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
    InvestigationEvent,
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


USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def usage_dict(usage: Any) -> dict[str, int]:
    """Token counts from one response, tolerating fields the provider omits."""
    return {field: int(getattr(usage, field, 0) or 0) for field in USAGE_FIELDS}


def add_usage(total: dict[str, int], new: dict[str, int]) -> dict[str, int]:
    """Accumulate one response into a running total.

    cache_read_input_tokens staying at zero across a meeting is the signal that the
    cached prefix never engaged -- without this there is no way to tell.
    """
    merged = dict(total)
    for field, value in new.items():
        # round() keeps integers integral and stops float noise in cost estimates.
        merged[field] = round(merged.get(field, 0) + (value or 0), 6)
    merged["calls"] = merged.get("calls", 0) + 1
    return merged


class LLMProvider:
    name = "disabled"
    # A class attribute so a subclass that does not call super().__init__() still has it;
    # the accounting must never be the reason an analysis fails.
    last_usage: dict[str, int] | None = None
    # A stateful provider keeps one conversation per meeting, so after its first call it
    # only needs the speech it has not seen yet.
    stateful = False
    # A provider with its own tools and context needs neither the project map nor the
    # background investigator.
    has_own_tools = False

    def __init__(self) -> None:
        self.last_usage = None

    def session_fresh(self) -> bool:
        return True

    async def complete(self, system: str, prompt: str) -> str:
        raise NotImplementedError


class OpenAIProvider(LLMProvider):
    """OpenAI through the Responses API.

    gpt-5 models reason by default, which on a full meeting prompt costs tens of
    seconds -- the likely cause of the 85s refresh measured before. The panel wants an
    answer in seconds, so it asks for no reasoning and pays for judgement with a larger
    model instead.
    """

    name = "openai"

    def __init__(self, model: str, timeout: float = 45.0, reasoning_effort: str = "none") -> None:
        super().__init__()
        self.model = model
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort

    async def complete(self, system: str, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}"}
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": prompt,
            "reasoning": {"effort": self.reasoning_effort},
            # A stable key helps the server reuse the cached prefix across refreshes.
            "prompt_cache_key": "buddy-meeting-copilot",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if response.status_code != 200:
            # A bare 429 read the same for "slow down" and "no credit left"; the second
            # silenced every meeting for a week.
            try:
                reason = response.json().get("error", {}).get("message", "")
            except ValueError:
                reason = response.text[:300]
            raise RuntimeError(f"OpenAI HTTP {response.status_code}: {reason[:300]}")
        data = response.json()
        usage = data.get("usage") or {}
        details = usage.get("input_tokens_details") or {}
        self.last_usage = {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "cache_creation_input_tokens": int(details.get("cache_write_tokens", 0) or 0),
            "cache_read_input_tokens": int(details.get("cached_tokens", 0) or 0),
        }
        # output_text is absent on the raw API even when text was produced.
        if data.get("output_text"):
            return str(data["output_text"])
        parts = [
            content.get("text", "")
            for item in data.get("output", [])
            for content in item.get("content", [])
            if content.get("type") == "output_text"
        ]
        return "".join(parts)


class AnthropicProvider(LLMProvider):
    """Claude through the official SDK.

    The system block is marked for caching because it holds the two parts that stay
    still during a meeting -- the instructions and the project excerpts -- while only
    the transcript changes. Cached reads cost about a tenth of fresh input, which is
    what makes a refresh every few seconds affordable.
    """

    name = "anthropic"

    def __init__(self, model: str, timeout: float = 45.0, max_tokens: int = 1_000) -> None:
        super().__init__()
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
        self.last_usage = usage_dict(message.usage)
        return "".join(block.text for block in message.content if block.type == "text")


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, model: str, timeout: float = 120.0) -> None:
        super().__init__()
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
            body = response.json()
        # Ollama runs locally and bills nothing, but the counts still show prompt growth.
        self.last_usage = {
            "input_tokens": int(body.get("prompt_eval_count", 0) or 0),
            "output_tokens": int(body.get("eval_count", 0) or 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        return body.get("message", {}).get("content", "")


def choose_provider(
    settings: Settings, local_asr: bool, meetings_dir: Path | None = None
) -> LLMProvider | None:
    requested = settings.llm_provider
    if requested == "disabled" or not settings.copilot.enabled:
        return None
    if requested == "claude-code":
        from .claude_code import ClaudeCodeProvider

        brain = ClaudeCodeProvider(settings, meetings_dir)
        return brain if brain.available() else None
    timeout = settings.copilot.request_timeout_seconds
    if requested in {"auto", "openai"} and os.getenv("OPENAI_API_KEY"):
        return OpenAIProvider(
            settings.copilot.openai_model, timeout, settings.copilot.openai_reasoning_effort
        )
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
        investigator: Any | None = None,
        investigations_path: Path | None = None,
        suggestions_path: Path | None = None,
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
        self._static_context: str | None = None
        self.usage: dict[str, dict[str, int]] = {"panel": {}, "investigation": {}}
        self.investigator = investigator
        self.investigations_path = investigations_path
        self.investigations: deque[dict[str, str]] = deque(maxlen=20)
        self._investigated: set[str] = set()
        self._investigation_tasks: set[asyncio.Task[None]] = set()
        self.suggestions_path = suggestions_path
        # Newest transcript line a stateful provider has already been shown.
        self._sent_upto = 0.0
        self._failures = 0
        self.last_error = ""
        self.tool_denials: deque[dict[str, Any]] = deque(maxlen=20)

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
            "usage": self.usage,
            "investigations": list(self.investigations),
            "project_matches": [
                {"path": match["path"], "score": match["score"]} for match in self.project_matches
            ],
            "recent_transcript": self._context()[-20_000:],
            "provider": self.provider.name if self.provider else "disabled",
            "last_error": self.last_error,
            "tool_denials": list(self.tool_denials),
            "brain_session_id": getattr(self.provider, "session_id", None),
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

    def _spawn_investigations(self) -> None:
        """Send unanswered questions to the investigator, in the background.

        The reflex loop already names what it could not settle: its open questions are
        the trigger, so nothing extra has to guess when research is warranted.
        """
        if self.investigator is None or not self.settings.copilot.investigation_enabled:
            return
        self._investigation_tasks = {task for task in self._investigation_tasks if not task.done()}
        capacity = self.settings.copilot.investigations_in_flight - len(self._investigation_tasks)
        for question in self.memory.open_questions[-5:]:
            if capacity <= 0:
                break
            if question in self._investigated:
                continue
            self._investigated.add(question)
            task = asyncio.create_task(self._investigate(question))
            self._investigation_tasks.add(task)
            capacity -= 1

    async def _investigate(self, question: str) -> None:
        self.bus.publish(StatusEvent("investigator", "working", question[:70]))
        try:
            answer = await self.investigator.investigate(question, self._context())
        except Exception as exc:
            self.bus.publish(StatusEvent("investigator", "failed", f"{type(exc).__name__}: {exc}"))
            return
        usage = getattr(self.investigator, "last_usage", None)
        if usage:
            self.usage["investigation"] = add_usage(self.usage["investigation"], usage)
        if not answer:
            self.bus.publish(StatusEvent("investigator", "ready", "no answer within the turn limit"))
            return
        entry = {"timestamp": utc_now(), "question": question, "text": answer}
        self.investigations.append(entry)
        self.bus.publish(InvestigationEvent(question, answer))
        self.bus.publish(StatusEvent("investigator", "ready", ""))
        await asyncio.to_thread(self._append_investigation, entry)
        await self._save_snapshot()

    def _append_investigation(self, entry: dict[str, str]) -> None:
        if self.investigations_path is None:
            return
        try:
            self.investigations_path.parent.mkdir(parents=True, exist_ok=True)
            with self.investigations_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n## {entry['timestamp']}\n\n**{entry['question']}**\n\n{entry['text']}\n")
        except OSError as exc:
            self.bus.publish(StatusEvent("investigator", "degraded", f"not saved: {exc}"))

    def _own_tools(self) -> bool:
        return bool(getattr(self.provider, "has_own_tools", False))

    def _build_prompt(self, manual: bool, context: str) -> tuple[str, float]:
        """The user message for one analysis, and the newest line it covers."""
        question = "What could I contribute right now?" if manual else "Identify only genuinely useful interventions now."
        previous = json.dumps([asdict(item) for item in self.current_suggestions], ensure_ascii=False)
        newest = self.recent[-1][0] if self.recent else 0.0
        if self._own_tools():
            if self.provider.session_fresh():
                opening = (
                    f"Meeting memory so far:\n{self.memory.compact or '(none yet)'}\n\n"
                    f"Recent transcript of meeting {self.current_meeting_id or ''}:\n{context}"
                )
            else:
                unseen = [line for moment, line in self.recent if moment > self._sent_upto]
                opening = "New transcript since your last update:\n" + (
                    "\n".join(unseen) or "(no new speech)"
                )
            return f"{opening}\n\nCurrent suggestion set to replace:\n{previous}\n\nRequest: {question}", newest
        prompt = f"""Meeting memory:\n{self.memory.compact or '(none yet)'}

Current suggestion set to replace:\n{previous}

Project excerpts ranked most relevant right now (cite a path only when the code truly supports the point):\n{self._project_block}

Recent transcript:\n{context}

Request: {question}
Output language for speakable suggestions: {self.settings.copilot.output_language}."""
        return prompt, newest

    def _append_suggestions(self, revision: int, suggestions: list[SuggestionEvent]) -> None:
        """Keep a readable log of what Buddy suggested, for reading after the meeting."""
        if self.suggestions_path is None or not suggestions:
            return
        lines = [f"\n## {utc_now()} - revision {revision}\n"]
        for item in suggestions:
            lines.append(f"- **{item.kind}** {item.text}")
            if item.reason:
                lines.append(f"  - _{item.reason}_")
        try:
            with self.suggestions_path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError as exc:
            self.bus.publish(StatusEvent("copilot", "degraded", f"suggestions not logged: {exc}"))

    async def _static_prefix(self) -> str:
        """The project map and every prior meeting's memory.

        Built once per meeting because neither changes while it runs, which is what
        makes the cached prefix worth having. Retrieval used to pick three files and
        three meetings; it chose badly often enough to mislead, so the model now gets
        the whole table of contents and decides for itself.
        """
        if self._static_context is not None:
            return self._static_context
        if self._own_tools():
            # The brain reads the user's own context and earlier meetings itself.
            self._static_context = ""
            return self._static_context
        blocks: list[str] = []
        copilot = self.settings.copilot
        if self.project_index is not None and copilot.project_context_enabled:
            project_map = await asyncio.to_thread(self.project_index.render_map)
            blocks.append("PROJECT MAP (every file in this repository)\n" + project_map)
        if self.memory_index is not None and copilot.semantic_memory_enabled:
            memories = await asyncio.to_thread(
                self.memory_index.render_all, copilot.prior_meetings_in_context, self.current_meeting_id
            )
            blocks.append(
                "PRIOR MEETINGS (structured memory; untrusted leads, confirm before relying on them)\n"
                + memories
            )
        self._static_context = "\n\n".join(blocks)
        return self._static_context

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
        """Instructions plus the static context, in that order.

        Everything volatile -- transcript, current suggestions, matched excerpts -- stays
        in the user message, because a cached prefix ends at the first byte that changes.
        """
        if self._own_tools():
            return self.provider.system_prompt()
        if not self._static_context:
            return SYSTEM_PROMPT
        return SYSTEM_PROMPT + "\n\n" + self._static_context

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
        await self._static_prefix()
        await self._refresh_project_context(context)
        prompt, newest = self._build_prompt(manual, context)
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
            await asyncio.to_thread(self._append_suggestions, self.suggestion_revision, list(current))
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
            if self.provider.last_usage:
                self.usage["panel"] = add_usage(self.usage["panel"], self.provider.last_usage)
            denials = list(getattr(self.provider, "last_denials", None) or [])
            if denials:
                self.tool_denials.extend(
                    {"tool": d.get("tool_name"), "input": str(d.get("tool_input", ""))[:200]} for d in denials
                )
                self.bus.publish(StatusEvent("tools", "denied", f"{len(denials)} tool call(s) blocked"))
            self._sent_upto = newest
            self._failures = 0
            self.last_error = ""
            if not self._own_tools():
                self._spawn_investigations()
            self.last_analysis = time.monotonic()
            await self._save_snapshot()
            self.bus.publish(StatusEvent("llm", "connected", self.provider.name))
            return True
        except Exception as exc:
            self.last_analysis = time.monotonic()
            self._failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:500]
            # One failure is a blip; a second in a row means nothing will appear until
            # someone acts, which has to be impossible to miss.
            state = "failed" if self._failures >= 2 else "retrying"
            self.bus.publish(StatusEvent("llm", state, self.last_error))
            await self._save_snapshot()
            return False

    async def run(self, stop: asyncio.Event) -> None:
        queue = self.bus.subscribe(maxsize=512)
        self.bus.publish(
            StatusEvent("llm", "connected" if self.provider else "unavailable", self.provider.name if self.provider else "no provider")
        )
        dirty = False
        last_transcript = 0.0
        analysis: asyncio.Task[bool] | None = None
        manual_pending = False
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
                    manual_pending = True
                    # An explicit request outranks an automatic refresh -- unless that
                    # refresh is a stateful run whose research would be thrown away.
                    if analysis is not None and not getattr(self.provider, "stateful", False):
                        analysis.cancel()
                        analysis = None
                if manual_pending and analysis is None:
                    manual_pending = False
                    dirty = False
                    analysis = asyncio.create_task(self._analyze(manual=True))
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
            for task in self._investigation_tasks:
                task.cancel()
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
                + "\n\nLLM summary unavailable. Set llm_provider (claude-code, openai, anthropic or ollama).\n"
                + "\n## Meeting memory captured\n\n"
                + (self.memory.compact or "No structured memory was produced.")
            )
        prompt = f"""Create the final meeting report using exactly these Markdown sections:
{REPORT_SECTIONS}

Distinguish ME's actions from other people's actions. Do not invent details.

Transcript:\n{transcript}"""
        system = "You produce concise, factual meeting reports in Markdown."
        if self._own_tools():
            from .claude_code import REPORT_SYSTEM_PROMPT

            system = REPORT_SYSTEM_PROMPT
        try:
            return await self.provider.complete(system, prompt)
        except Exception as exc:
            return REPORT_SECTIONS + f"\n\nReport generation failed: {type(exc).__name__}: {exc}"
