from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .events import EventBus, StatusEvent, SuggestionEvent, TranscriptEvent


SYSTEM_PROMPT = """You are a real-time meeting copilot. Be selective and useful, never generic.
Suggestions must be short, directly speakable, and in English unless configured otherwise.
Return strict JSON with keys: suggestions (array), memory_update (string), decisions (array),
action_items (array), open_questions (array). Each suggestion has kind (COMMENT, QUESTION,
RISK, CONNECTION, or ACTION), text, and reason. Return zero suggestions if none is valuable.
Do not invent facts."""

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


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str) -> None:
        self.model = model

    async def complete(self, system: str, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
        payload = {"model": self.model, "instructions": system, "input": prompt}
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("output_text"):
            return str(data["output_text"])
        parts: list[str] = []
        for output in data.get("output", []):
            for content in output.get("content", []):
                if content.get("type") == "output_text":
                    parts.append(content.get("text", ""))
        return "".join(parts)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str) -> None:
        self.model = model

    async def complete(self, system: str, prompt: str) -> str:
        headers = {
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": self.model,
            "max_tokens": 900,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        return "".join(x.get("text", "") for x in data.get("content", []) if x.get("type") == "text")


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, model: str) -> None:
        self.model = model
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")

    async def complete(self, system: str, prompt: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "options": {"temperature": 0.2},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{self.base_url}/api/chat", json=payload)
            response.raise_for_status()
            return response.json().get("message", {}).get("content", "")


def choose_provider(settings: Settings, local_asr: bool) -> LLMProvider | None:
    requested = settings.llm_provider
    if requested == "disabled" or not settings.copilot.enabled:
        return None
    if requested in {"auto", "openai"} and os.getenv("OPENAI_API_KEY"):
        return OpenAIProvider(settings.copilot.openai_model)
    if requested in {"auto", "anthropic"} and os.getenv("ANTHROPIC_API_KEY"):
        return AnthropicProvider(settings.copilot.anthropic_model)
    # A local LLM is never auto-selected while CPU ASR has absolute priority.
    if requested == "ollama":
        return OllamaProvider(settings.copilot.ollama_model)
    return None


TRIGGER = re.compile(
    r"\?|\b(decide|decision|deadline|due|risk|issue|problem|blocked|owner|action item|"
    r"what do you think|your opinion|validate|production|incident|responsible|follow.?up)\b",
    re.IGNORECASE,
)


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

    def __post_init__(self) -> None:
        self.decisions = self.decisions or []
        self.action_items = self.action_items or []
        self.open_questions = self.open_questions or []


class CopilotWorker:
    def __init__(self, settings: Settings, provider: LLMProvider | None, bus: EventBus, snapshot_path: Path | None = None) -> None:
        self.settings = settings
        self.provider = provider
        self.bus = bus
        self.manual: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self.recent: deque[tuple[float, str]] = deque()
        self.memory = MeetingMemory()
        self.last_analysis = 0.0
        self.snapshot_path = snapshot_path
        self.suggestions: deque[dict[str, str]] = deque(maxlen=20)

    def _write_snapshot(self) -> None:
        if self.snapshot_path is None:
            return
        payload = {
            "memory": self.memory.compact,
            "decisions": self.memory.decisions[-50:],
            "action_items": self.memory.action_items[-50:],
            "open_questions": self.memory.open_questions[-50:],
            "recent_suggestions": list(self.suggestions),
            "recent_transcript": self._context()[-20_000:],
            "provider": self.provider.name if self.provider else "disabled",
        }
        temporary = self.snapshot_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.snapshot_path)

    def suggest_now(self) -> None:
        if self.manual.empty():
            self.manual.put_nowait(None)

    def _context(self) -> str:
        cutoff = time.monotonic() - self.settings.copilot.context_minutes * 60
        while self.recent and self.recent[0][0] < cutoff:
            self.recent.popleft()
        return "\n".join(line for _, line in self.recent)

    async def _analyze(self, manual: bool) -> None:
        if self.provider is None:
            self.bus.publish(StatusEvent("llm", "unavailable", "Configure an API provider"))
            return
        context = self._context()
        if not context:
            return
        question = "What could I contribute right now?" if manual else "Identify only genuinely useful interventions now."
        prompt = f"""Meeting memory:\n{self.memory.compact or '(none yet)'}

Recent transcript:\n{context}

Request: {question}
Output language for speakable suggestions: {self.settings.copilot.output_language}."""
        self.bus.publish(StatusEvent("llm", "working", "manual" if manual else "automatic"))
        try:
            raw = await self.provider.complete(SYSTEM_PROMPT, prompt)
            data = _extract_json(raw)
            for item in data.get("suggestions", [])[:3]:
                text = str(item.get("text", "")).strip()
                if text:
                    suggestion = {"kind": str(item.get("kind", "COMMENT")).upper(), "text": text, "reason": str(item.get("reason", ""))}
                    self.suggestions.append(suggestion)
                    self.bus.publish(
                        SuggestionEvent(suggestion["kind"], suggestion["text"], suggestion["reason"])
                    )
            self.memory.compact = str(data.get("memory_update") or self.memory.compact)
            for attr, key in (
                ("decisions", "decisions"),
                ("action_items", "action_items"),
                ("open_questions", "open_questions"),
            ):
                values = getattr(self.memory, attr)
                for value in data.get(key, []):
                    if value not in values:
                        values.append(str(value))
            self.last_analysis = time.monotonic()
            await asyncio.to_thread(self._write_snapshot)
            self.bus.publish(StatusEvent("llm", "connected", self.provider.name))
        except Exception as exc:
            self.bus.publish(StatusEvent("llm", "retrying", f"{type(exc).__name__}: {exc}"))

    async def run(self, stop: asyncio.Event) -> None:
        queue = self.bus.subscribe(maxsize=512)
        self.bus.publish(
            StatusEvent("llm", "connected" if self.provider else "unavailable", self.provider.name if self.provider else "no provider")
        )
        try:
            while not stop.is_set():
                event_task = asyncio.create_task(queue.get())
                manual_task = asyncio.create_task(self.manual.get())
                done, pending = await asyncio.wait(
                    {event_task, manual_task}, timeout=0.25, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if not done:
                    continue
                if manual_task in done:
                    await self._analyze(manual=True)
                    continue
                event = event_task.result()
                if not isinstance(event, TranscriptEvent) or not event.final:
                    continue
                line = f"{event.speaker}: {event.text}"
                self.recent.append((time.monotonic(), line))
                await asyncio.to_thread(self._write_snapshot)
                cooldown = self.settings.copilot.suggestion_cooldown_seconds
                relevant = bool(TRIGGER.search(event.text))
                if (
                    self.settings.copilot.automatic_suggestions
                    and relevant
                    and time.monotonic() - self.last_analysis >= cooldown
                ):
                    await self._analyze(manual=False)
        finally:
            self.bus.unsubscribe(queue)

    async def final_report(self, transcript: str) -> str:
        if not transcript.strip():
            return REPORT_SECTIONS + "\n\nNo speech was transcribed."
        if self.provider is None:
            return (
                REPORT_SECTIONS
                + "\n\nLLM summary unavailable. Configure OPENAI_API_KEY, ANTHROPIC_API_KEY, or explicit Ollama.\n"
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
