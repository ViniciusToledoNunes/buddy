"""The work board: what is being done now, what comes next, and what is blocked.

A few short lines at the top of Buddy's window, so the user sees at a glance where their work
stands. The Claude Code session keeps it current with `buddy board` as things happen (a task
picked up, a blocker found or cleared, a PR merged); a Jira ticket, when there is one, is shown
as its key with its status. It is a view, not a log: detail belongs in the ticket or in notes.

It lives in `<runtime>/board.json`, written whole each time (it is small).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUSES = ("now", "next", "blocked", "done")
MAX_TITLE = 120
MAX_NOTE = 200
TICKET = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Item:
    id: int
    title: str
    status: str = "next"
    ticket: str = ""  # "QCMVEN-4625"
    link: str = ""
    blocker: str = ""  # why it is blocked, in a few words
    note: str = ""  # one short line: the next step, a PR number
    ticket_status: str = ""  # from Jira, when someone keeps it in sync
    ticket_assignee: str = ""
    order: int = 0
    updated: str = field(default_factory=_now)
    done_at: str = ""

    def to_dict(self, ticket_url: str = "") -> dict[str, Any]:
        data = asdict(self)
        if not self.link and self.ticket and ticket_url:
            data["link"] = ticket_url.replace("{key}", self.ticket)
        return data


def _short(text: str, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


class Board:
    def __init__(self, runtime_dir: Path) -> None:
        self.path = runtime_dir / "board.json"

    def items(self) -> list[Item]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = []
        for raw in data.get("items", []) if isinstance(data, dict) else []:
            try:
                items.append(Item(**{key: raw[key] for key in Item.__dataclass_fields__ if key in raw}))
            except TypeError:
                continue
        return items

    def _write(self, items: list[Item]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = {"updated": _now(), "items": [asdict(item) for item in items]}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def _find(self, items: list[Item], key: str | int) -> Item:
        text = str(key).strip()
        for item in items:
            if text == str(item.id) or (item.ticket and text.upper() == item.ticket):
                return item
        raise LookupError(f"no board item {key}")

    @staticmethod
    def _check(status: str | None, ticket: str | None) -> None:
        if status is not None and status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        if ticket and not TICKET.match(ticket.upper()):
            raise ValueError(f"{ticket} does not look like a ticket key (e.g. QCMVEN-4625)")

    def add(self, title: str, status: str = "next", ticket: str = "", link: str = "", blocker: str = "", note: str = "") -> Item:
        self._check(status, ticket)
        title = _short(title, MAX_TITLE)
        if not title:
            raise ValueError("a board item needs a title")
        items = self.items()
        item = Item(
            id=max([i.id for i in items] + [0]) + 1, title=title, status=status, ticket=ticket.upper(), link=link.strip(),
            blocker=_short(blocker, MAX_NOTE), note=_short(note, MAX_NOTE), order=max([i.order for i in items] + [0]) + 1,
        )
        if status == "done":
            item.done_at = _now()
        items.append(item)
        self._write(items)
        return item

    def set(
        self,
        key: str | int,
        title: str | None = None,
        status: str | None = None,
        ticket: str | None = None,
        link: str | None = None,
        blocker: str | None = None,
        note: str | None = None,
        ticket_status: str | None = None,
        ticket_assignee: str | None = None,
    ) -> Item:
        self._check(status, ticket)
        items = self.items()
        item = self._find(items, key)
        if title is not None:
            item.title = _short(title, MAX_TITLE) or item.title
        if status is not None and status != item.status:
            item.status = status
            item.done_at = _now() if status == "done" else ""
            if status != "blocked" and blocker is None:
                item.blocker = ""  # unblocked: the old reason no longer applies
        for name, value, limit in (("blocker", blocker, MAX_NOTE), ("note", note, MAX_NOTE),
                                   ("ticket_status", ticket_status, 40), ("ticket_assignee", ticket_assignee, 60)):
            if value is not None:
                setattr(item, name, _short(value, limit))
        if ticket is not None:
            item.ticket = ticket.upper()
        if link is not None:
            item.link = link.strip()
        item.updated = _now()
        self._write(items)
        return item

    def remove(self, key: str | int) -> Item:
        items = self.items()
        item = self._find(items, key)
        self._write([i for i in items if i is not item])
        return item

    def tickets(self) -> list[str]:
        """The tickets on the board that are not done, for a job that syncs their status."""
        return sorted({i.ticket for i in self.items() if i.ticket and i.status != "done"})

    def view(self, ticket_url: str = "", today: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """What the window shows: each column in order; done only if finished today."""
        today = today or datetime.now().astimezone().date().isoformat()
        columns: dict[str, list[dict[str, Any]]] = {status: [] for status in STATUSES}
        for item in sorted(self.items(), key=lambda i: i.order):
            if item.status == "done":
                try:
                    finished = datetime.fromisoformat(item.done_at).astimezone().date().isoformat()
                except ValueError:
                    continue
                if finished != today:
                    continue
            columns[item.status].append(item.to_dict(ticket_url))
        return columns

    def prune_done(self, keep_days: int = 7) -> int:
        """Forget items done more than a week ago."""
        items, kept = self.items(), []
        for item in items:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(item.done_at)).days if item.done_at else 0
            except ValueError:
                age = 0
            if item.status == "done" and age > keep_days:
                continue
            kept.append(item)
        if len(kept) != len(items):
            self._write(kept)
        return len(items) - len(kept)
