from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import psutil

from .config import Settings, project_root

SpeakerFilter = Literal["all", "ME", "REMOTE"]


class MeetingRepository:
    """Bounded, path-safe access to Buddy's persisted meeting context."""

    def __init__(self, meetings_dir: Path, runtime_dir: Path) -> None:
        self.meetings_dir = meetings_dir.expanduser().resolve()
        self.runtime_dir = runtime_dir.expanduser().resolve()

    @classmethod
    def from_settings(cls, settings: Settings) -> "MeetingRepository":
        meetings = Path(settings.meetings_dir)
        if not meetings.is_absolute():
            meetings = project_root() / meetings
        return cls(meetings, project_root() / ".meeting-agent")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def status(self) -> dict[str, Any]:
        state = self._read_json(self.runtime_dir / "state.json")
        if not state:
            return {"active": False, "recording": False}
        try:
            pid = int(state.get("pid", 0))
            process = psutil.Process(pid)
            started = datetime.fromisoformat(str(state["started"]).replace("Z", "+00:00"))
            same_process = abs(process.create_time() - started.timestamp()) < 60
            if not process.is_running() or not same_process:
                return {"active": False, "recording": False, "stale_state": True, "stale_pid": pid}
        except (psutil.Error, OSError, ValueError, TypeError, KeyError):
            return {"active": False, "recording": False, "stale_state": True, "stale_pid": state.get("pid")}
        return {"active": True, **state}

    def _directories(self) -> list[Path]:
        if not self.meetings_dir.exists():
            return []
        return sorted((path for path in self.meetings_dir.iterdir() if path.is_dir()), reverse=True)

    def _resolve(self, meeting_id: str) -> Path:
        if meeting_id == "latest":
            active_dir = self.status().get("meeting_dir")
            if active_dir:
                candidate = Path(str(active_dir)).resolve()
            else:
                directories = self._directories()
                if not directories:
                    raise ValueError("No meetings are available")
                candidate = directories[0]
        else:
            if not meeting_id or meeting_id in {".", ".."} or any(x in meeting_id for x in ("/", "\\")):
                raise ValueError("Invalid meeting_id")
            candidate = (self.meetings_dir / meeting_id).resolve()
        try:
            candidate.relative_to(self.meetings_dir)
        except ValueError as exc:
            raise ValueError("Meeting path is outside the configured meetings directory") from exc
        if not candidate.is_dir():
            raise ValueError(f"Meeting not found: {meeting_id}")
        return candidate

    def list_meetings(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 50))
        items: list[dict[str, Any]] = []
        for directory in self._directories()[:limit]:
            metadata = self._read_json(directory / "metadata.json")
            summary = ""
            try:
                summary = (directory / "summary.md").read_text(encoding="utf-8")[:500]
            except OSError:
                pass
            items.append({
                "meeting_id": directory.name,
                "started_at": metadata.get("started_at"),
                "stopped_at": metadata.get("stopped_at"),
                "recording": bool(metadata.get("recording", False)),
                "final_transcript_events": metadata.get("final_transcript_events"),
                "summary_preview": summary,
            })
        return items

    def transcript(
        self,
        meeting_id: str = "latest",
        *,
        max_events: int = 500,
        speaker: SpeakerFilter = "all",
        minutes: int | None = None,
    ) -> list[dict[str, Any]]:
        directory = self._resolve(meeting_id)
        path = directory / "transcript.jsonl"
        max_events = max(1, min(max_events, 2_000))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, min(minutes, 120))) if minutes else None
        events: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or (speaker != "all" and event.get("speaker") != speaker):
                continue
            if cutoff:
                try:
                    timestamp = datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))
                    if timestamp < cutoff:
                        break
                except (KeyError, ValueError, TypeError):
                    pass
            events.append(event)
            if len(events) >= max_events:
                break
        return list(reversed(events))

    def meeting(self, meeting_id: str = "latest", include_transcript: bool = True, max_events: int = 500) -> dict[str, Any]:
        directory = self._resolve(meeting_id)
        result: dict[str, Any] = {
            "meeting_id": directory.name,
            "metadata": self._read_json(directory / "metadata.json"),
        }
        for filename, key, maximum in (("summary.md", "summary", 50_000), ("copilot.json", "copilot", 100_000)):
            try:
                text = (directory / filename).read_text(encoding="utf-8")[:maximum]
                result[key] = json.loads(text) if filename.endswith(".json") else text
            except (OSError, ValueError):
                result[key] = {} if filename.endswith(".json") else ""
        if include_transcript:
            result["transcript"] = self.transcript(directory.name, max_events=max_events)
        return result

    def live_transcript(self, minutes: int = 5, max_events: int = 200, speaker: SpeakerFilter = "all") -> dict[str, Any]:
        state = self.status()
        if not state["active"]:
            return {"active": False, "events": []}
        return {
            "active": True,
            "meeting_id": Path(str(state["meeting_dir"])).name,
            "events": self.transcript("latest", max_events=max_events, speaker=speaker, minutes=minutes),
        }

    def copilot_context(self, meeting_id: str = "latest") -> dict[str, Any]:
        directory = self._resolve(meeting_id)
        return self._read_json(directory / "copilot.json")

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        query = query.strip()
        if len(query) < 2:
            raise ValueError("query must contain at least 2 characters")
        needle = query.casefold()[:200]
        limit = max(1, min(limit, 100))
        hits: list[dict[str, Any]] = []
        for directory in self._directories()[:100]:
            for event in self.transcript(directory.name, max_events=2_000):
                text = str(event.get("text", ""))
                if needle in text.casefold():
                    hits.append({
                        "meeting_id": directory.name, "speaker": event.get("speaker"),
                        "timestamp": event.get("timestamp"), "text": text,
                    })
                    if len(hits) >= limit:
                        return hits
        return hits

    def request_suggestion(self) -> dict[str, Any]:
        if not self.status()["active"]:
            return {"accepted": False, "reason": "no active meeting"}
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "suggest.flag").touch()
        return {"accepted": True}

    def request_stop(self) -> dict[str, Any]:
        if not self.status()["active"]:
            return {"accepted": False, "reason": "no active meeting"}
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "stop.flag").touch()
        return {"accepted": True}
