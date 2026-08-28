from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Any


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on",
    "or", "that", "the", "this", "to", "was", "we", "will", "with", "you",
    "a", "ao", "aos", "as", "com", "como", "da", "das", "de", "do", "dos", "e", "em", "entre",
    "esta", "este", "isso", "na", "nas", "no", "nos", "o", "os", "ou", "para", "por", "que", "se",
    "sem", "um", "uma",
}


def _tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", ascii_text)
        if token not in STOP_WORDS
    }


class MeetingMemoryIndex:
    """Local retrieval over structured meeting memories; raw transcripts are never loaded."""

    def __init__(self, meetings_dir: Path, max_meetings: int = 50) -> None:
        self.meetings_dir = meetings_dir.expanduser().resolve()
        self.max_meetings = max(1, min(max_meetings, 500))

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _documents(self, exclude_meeting_id: str | None) -> list[dict[str, Any]]:
        if not self.meetings_dir.exists():
            return []
        documents: list[dict[str, Any]] = []
        directories = sorted((path for path in self.meetings_dir.iterdir() if path.is_dir()), reverse=True)
        for directory in directories[: self.max_meetings]:
            if directory.name == exclude_meeting_id:
                continue
            copilot = self._read_json(directory / "copilot.json")
            memory = str(copilot.get("memory", "")).strip()
            topics = [str(value).strip() for value in copilot.get("topics", []) if str(value).strip()][:30]
            decisions = [str(value).strip() for value in copilot.get("decisions", []) if str(value).strip()][-20:]
            actions = [str(value).strip() for value in copilot.get("action_items", []) if str(value).strip()][-20:]
            questions = [str(value).strip() for value in copilot.get("open_questions", []) if str(value).strip()][-20:]
            if not any((memory, topics, decisions, actions, questions)):
                continue
            searchable = "\n".join([memory, *topics, *decisions, *actions, *questions])
            documents.append(
                {
                    "meeting_id": directory.name,
                    "memory": memory[:2_000],
                    "topics": topics,
                    "decisions": decisions,
                    "action_items": actions,
                    "open_questions": questions,
                    "tokens": _tokens(searchable),
                }
            )
        return documents

    def render_all(self, limit: int = 20, exclude_meeting_id: str | None = None) -> str:
        """Structured memory of every recent meeting, for the cached prefix.

        Retrieval picked three by term overlap and got it wrong often enough to mislead;
        the whole set is small, stable during a meeting, and lets the model decide what
        is relevant.
        """
        blocks = []
        for document in self._documents(exclude_meeting_id)[: max(1, limit)]:
            parts = [f"## {document['meeting_id']}"]
            if document["memory"]:
                parts.append(document["memory"])
            for label, key in (("Topics", "topics"), ("Decisions", "decisions"), ("Open questions", "open_questions")):
                values = document[key]
                if values:
                    parts.append(f"{label}: " + "; ".join(values))
            blocks.append("\n".join(parts))
        return "\n\n".join(blocks) if blocks else "(no prior meetings)"

    def find_related(
        self,
        query: str,
        limit: int = 3,
        exclude_meeting_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        documents = self._documents(exclude_meeting_id)
        if not documents:
            return []
        frequencies: dict[str, int] = {}
        for document in documents:
            for token in document["tokens"]:
                frequencies[token] = frequencies.get(token, 0) + 1
        ranked: list[dict[str, Any]] = []
        for document in documents:
            overlap = query_tokens & document["tokens"]
            if not overlap:
                continue
            weighted_overlap = sum(math.log((len(documents) + 1) / (frequencies[token] + 0.5)) + 1 for token in overlap)
            score = weighted_overlap / math.sqrt(max(1, len(query_tokens)) * max(1, len(document["tokens"])))
            ranked.append(
                {
                    key: value
                    for key, value in document.items()
                    if key != "tokens"
                }
                | {"score": round(score, 4)}
            )
        ranked.sort(key=lambda item: (-item["score"], item["meeting_id"]), reverse=False)
        return ranked[: max(1, min(limit, 10))]
