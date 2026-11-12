"""Actions the session proposes and the user approves with one click.

The Claude Code session watching Buddy often ends a reply with "want me to look at
this?" or a draft reply to a Slack thread. Each such proposal becomes an action: a card
in Buddy's window with what would be done, word for word when it leaves the machine,
and Approve / Edit / Dismiss. Approving sends `ACTION_APPROVED` to the session, which
does exactly that and reports back (`buddy action <id> done --result ...`), and the card
shows how it went.

Only the session (or the user, with `buddy propose`) creates actions: what someone else
wrote in Slack or said in a meeting never turns into one by itself. A click approves the
content shown on the card, edited or not; an action runs at most once.

A reply that only needs to be pasted (`mode: copy`, where nothing can send for the
user) is done by the window itself: approving copies the text and opens the thread.

Each action is one JSON file in `<runtime>/actions/`, so the session, the CLI and the
window never write the same file at the same time.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KINDS = ("reply", "review", "investigate", "merge", "post", "ticket", "run", "other")
STATUSES = ("proposed", "approved", "running", "done", "failed", "dismissed")
OPEN = ("proposed", "approved", "running")
MODES = ("run", "copy", "send")
MAX_TITLE = 160
MAX_DETAIL = 8000
STALE_HOURS = 20  # a proposal from yesterday may no longer fit the thread
KEEP_FINISHED_HOURS = 36


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_hours(stamp: str) -> float:
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


@dataclass
class Action:
    id: int
    kind: str
    title: str
    detail: str = ""  # exactly what will be sent, posted or done
    link: str = ""  # the thread, PR or ticket it is about
    mode: str = "run"  # run: the session does it; copy: copy + open; send: the session sends it
    outward: bool = False  # it leaves this machine (a message, a comment, a merge, a push)
    source: str = ""  # where it came from: "slack", "meeting", "session"
    status: str = "proposed"
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)
    result: str = ""
    result_link: str = ""

    @property
    def stale(self) -> bool:
        return self.status == "proposed" and _age_hours(self.created) >= STALE_HOURS

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "stale": self.stale}


class ActionStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.directory = runtime_dir / "actions"

    def _path(self, action_id: int) -> Path:
        return self.directory / f"{int(action_id):05d}.json"

    def _next_id(self) -> int:
        """Numbers keep growing across days, so "approve 12" can only mean one card."""
        self.directory.mkdir(parents=True, exist_ok=True)
        counter = self.directory / "next-id"
        for _ in range(50):
            try:
                current = int(counter.read_text(encoding="utf-8").strip() or "1")
            except (OSError, ValueError):
                current = max([a.id for a in self.all()] + [0]) + 1
            claim = self.directory / f".claim-{current}"
            try:
                fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # first writer wins
            except FileExistsError:
                counter.write_text(str(current + 1), encoding="utf-8")
                continue
            os.close(fd)
            counter.write_text(str(current + 1), encoding="utf-8")
            return current
        raise RuntimeError("could not number the action")

    def save(self, action: Action) -> Action:
        action.updated = _now()
        path = self._path(action.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(action), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return action

    def propose(
        self,
        kind: str,
        title: str,
        detail: str = "",
        link: str = "",
        mode: str = "run",
        outward: bool | None = None,
        source: str = "session",
    ) -> Action:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        title = " ".join(title.split())
        if not title:
            raise ValueError("an action needs a title")
        if outward is None:
            outward = kind in ("reply", "merge", "post", "ticket") or mode == "send"
        action = Action(
            id=self._next_id(), kind=kind, title=title[:MAX_TITLE], detail=detail[:MAX_DETAIL].strip(),
            link=link.strip(), mode=mode, outward=outward, source=source,
        )
        return self.save(action)

    def get(self, action_id: int) -> Action | None:
        try:
            data = json.loads(self._path(action_id).read_text(encoding="utf-8"))
            return Action(**{key: data[key] for key in Action.__dataclass_fields__ if key in data})
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def all(self) -> list[Action]:
        if not self.directory.exists():
            return []
        found = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                action = self.get(int(path.stem))
            except ValueError:
                continue
            if action is not None:
                found.append(action)
        return found

    def visible(self) -> list[Action]:
        """What the window shows: open actions, and finished ones for a day and a half."""
        return [a for a in self.all() if a.status in OPEN or _age_hours(a.updated) < KEEP_FINISHED_HOURS]

    def decide(self, action_id: int, approve: bool, detail: str | None = None) -> Action:
        """The user's click. Only a proposal can be decided, and only once."""
        action = self.get(action_id)
        if action is None:
            raise LookupError(f"no action {action_id}")
        if action.status != "proposed":
            raise ValueError(f"action {action_id} is already {action.status}")
        if detail is not None:
            action.detail = detail[:MAX_DETAIL].strip()
        action.status = "approved" if approve else "dismissed"
        return self.save(action)

    def report(self, action_id: int, status: str, result: str = "", link: str = "") -> Action:
        """The session saying how it went: running, done or failed."""
        if status not in ("running", "done", "failed"):
            raise ValueError("status must be running, done or failed")
        action = self.get(action_id)
        if action is None:
            raise LookupError(f"no action {action_id}")
        action.status = status
        if result:
            action.result = " ".join(result.split())[:600]
        if link:
            action.result_link = link.strip()
        return self.save(action)

    def prune(self, keep_days: float = 14) -> int:
        """Delete finished actions older than this; open ones always stay."""
        removed = 0
        for action in self.all():
            if action.status not in OPEN and _age_hours(action.updated) > keep_days * 24:
                self._path(action.id).unlink(missing_ok=True)
                removed += 1
        for claim in self.directory.glob(".claim-*") if self.directory.exists() else []:
            if time.time() - claim.stat().st_mtime > keep_days * 86400:
                claim.unlink(missing_ok=True)
        return removed


def approval_event(action: Action) -> dict[str, Any]:
    """The fields of the ACTION_APPROVED event the watching session acts on."""
    return {
        "id": action.id,
        "action": action.kind,
        "title": action.title,
        "detail": action.detail,
        "link": action.link,
        "mode": action.mode,
        "outward": action.outward,
    }
