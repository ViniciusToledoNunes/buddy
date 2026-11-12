"""Chores the brain does on the working memory: set it up on a new machine, keep it true.

The probe only looks: which tools exist, which logins are active, the *names* of
credential files and the repositories on disk. It never opens a credential file and
never prints a token, because its output goes to an LLM and may end up in the memory.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .config import Settings

TOOLS = (
    "git", "gh", "glab", "gcloud", "bq", "aws", "az", "kubectl", "terraform", "docker",
    "claude", "cursor-agent", "opencode", "codex", "jq", "node", "python3", "java",
)
# Home folders that hold the system's or the user's media and can be large enough to slow the
# probe down. On macOS, listing Desktop or Documents raises a privacy prompt out of nowhere.
NOT_REPO_ROOTS = {
    "Library", "Applications", "Downloads", "Movies", "Music", "Pictures", "Public", "AppData",
    "Videos", "snap", "Desktop", "Documents",
}
AGENT_PATHS = (
    ".claude/CLAUDE.md", ".claude/projects", ".claude/skills", ".cursor/projects",
    ".codex", ".config/opencode", ".local/share/opencode",
)
INSTRUCTION_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".cursor/rules", ".github/copilot-instructions.md")
CREDENTIAL_NAME = re.compile(r"\.env$|token|secret|cred|auth|key", re.IGNORECASE)
MAX_REPOS = 60
MAX_LISTED = 40
# A laptop asleep at 08:30 that opens at 09:40 still gets the morning refresh; one opened
# in the afternoon does not get a stale one on top of the 17:30 run.
CATCH_UP = timedelta(hours=3)
CHORE_TIMEOUT = 900.0  # the most both harness configs accept

ONBOARD_PROMPT = """Onboarding: set up the user's working memory from what is on this machine.

Below is a probe of the machine: tools, active logins, helper scripts, the NAMES of credential \
files (never open them), agent folders and repositories. Using it, the context folders and \
read-only commands only:
1. Write work/reference_access.md: one row per tool or service (code host, tickets, chat, cloud, \
monitoring, databases, the agents themselves): how to reach it, what is read-only and what writes, \
and where the detail lives (CLAUDE.md, AGENTS.md, rules files, a helper's usage text). Check each \
access with a harmless read when you can, and say when you could not.
2. Fill global/: who the user is, their role and how they want things done, from their agent \
instruction files and past agent conversations (one file per preference or correction).
3. Fill work/: their projects and repositories, team, and current plan in work/project_open_items.md \
(in progress, next, waiting on), from their repositories, pull requests, tickets and recent meetings.
Follow CONVENTIONS.md and keep both MEMORY.md indexes current. When a file exists, update it and keep \
what is still true. Never copy a secret, a token or raw business data into the memory, and never \
change anything outside it. Reply with a short list of what you wrote and what you could not reach.

Probe:
{probe}"""

REFRESH_PROMPT = """Scheduled memory refresh ({slot}). Check the working memory against the real \
state of the work and fix it.

Start from work/project_open_items.md and the files it points to. Using read-only commands (see \
work/reference_access.md for how to reach each tool): the user's open and recently merged pull \
requests, their tickets and status changes, recent commits in their repositories, meetings since \
the last refresh ({meetings}: summaries, transcripts and screenshots), Slack threads still open, \
and the cards on Buddy's window (`{buddy} actions --all`). Then:
- rewrite the plan: what is in progress, what comes next, what waits on someone (and on whom);
- move finished items to done, and mark as unconfirmed anything you could not check;
- fix references that changed (branch, pull request, ticket status, owner);
- keep the work board on Buddy's window in line with the plan.
Follow CONVENTIONS.md. Never change anything outside the memory and the board. Reply with one \
short line per change, or exactly NOTHING when everything was already true."""


def _run(command: list[str], timeout: float = 10) -> tuple[int, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return done.returncode, (done.stdout + done.stderr).strip()


def _without_credentials(url: str) -> str:
    """A remote like https://user:token@host/... must not reach the prompt."""
    return re.sub(r"(?<=://)[^/@\s]+@", "", url)


def _logins() -> dict[str, str]:
    logins: dict[str, str] = {}
    if shutil.which("gh"):
        code, out = _run(["gh", "auth", "status"])
        accounts = re.findall(r"account (\S+)", out)
        logins["gh"] = f"logged in as {', '.join(dict.fromkeys(accounts))}" if accounts else (
            "not logged in" if code else "logged in")
    if shutil.which("gcloud"):
        code, out = _run(["gcloud", "config", "list", "--format=json"], timeout=20)
        try:
            core = json.loads(out).get("core", {}) if code == 0 else {}
        except ValueError:
            core = {}
        logins["gcloud"] = (
            f"account {core.get('account', '?')}, project {core.get('project', '?')}" if core else "no account set"
        )
    return logins


def _helpers(home: Path) -> list[str]:
    found: list[str] = []
    for folder in (home / "bin", home / ".local" / "bin"):
        if folder.is_dir():
            found += sorted(
                f"{folder.name}/{entry.name}" for entry in folder.iterdir()
                if entry.is_file() and os.access(entry, os.X_OK) and entry.suffix in {"", ".sh", ".py"}
            )
    return found[:MAX_LISTED]


def _credential_names(home: Path) -> list[str]:
    folder = home / ".config"
    if not folder.is_dir():
        return []
    names = sorted(
        str(entry.relative_to(home)) for entry in folder.iterdir()
        if entry.is_file() and CREDENTIAL_NAME.search(entry.name)
    )
    return names[:MAX_LISTED]


def _repo(path: Path, home: Path) -> dict[str, Any]:
    _, remote = _run(["git", "-C", str(path), "remote", "get-url", "origin"], timeout=5)
    _, branch = _run(["git", "-C", str(path), "branch", "--show-current"], timeout=5)
    _, last = _run(["git", "-C", str(path), "log", "-1", "--format=%cs"], timeout=5)
    return {
        "path": f"~/{path.relative_to(home)}",
        "remote": _without_credentials(remote) if "://" in remote or "@" in remote else "",
        "branch": branch,
        "last_commit": last,
        "instructions": [name for name in INSTRUCTION_FILES if (path / name).exists()],
    }


def _repositories(home: Path) -> list[dict[str, Any]]:
    found: list[Path] = []
    roots = [
        entry for entry in sorted(home.iterdir())
        if entry.is_dir() and not entry.name.startswith(".") and entry.name not in NOT_REPO_ROOTS
    ]
    for base in roots:
        if len(found) >= MAX_REPOS:
            break
        if (base / ".git").exists():
            found.append(base)
            continue
        with suppress(OSError):  # a folder the user cannot list (macOS privacy) is skipped
            for child in sorted(base.iterdir()):
                if (child / ".git").exists():
                    found.append(child)
                elif child.is_dir() and not child.name.startswith("."):
                    with suppress(OSError):
                        found += [grand for grand in sorted(child.iterdir()) if (grand / ".git").exists()][:20]
                if len(found) >= MAX_REPOS:
                    break
    return [_repo(path, home) for path in found[:MAX_REPOS]]


def probe_machine(home: Path | None = None) -> dict[str, Any]:
    """What this machine gives the brain to work with: names and states, never secrets."""
    home = home or Path.home()
    return {
        "system": f"{platform.system()} {platform.release()} {platform.machine()}",
        "user": home.name,
        "tools": [tool for tool in TOOLS if shutil.which(tool)],
        "logins": _logins(),
        "helpers": _helpers(home),
        "credential_files_names_only": _credential_names(home),
        "agent_folders": [f"~/{path}" for path in AGENT_PATHS if (home / path).exists()],
        "repositories": _repositories(home),
    }


def for_chores(settings: Settings) -> Settings:
    """The same brain with the longest call the harnesses allow: a refresh reads many sources."""
    copilot = settings.copilot.model_copy(update={"claude_timeout_seconds": CHORE_TIMEOUT})
    cursor = settings.brain.cursor.model_copy(update={"timeout_seconds": CHORE_TIMEOUT})
    brain = settings.brain.model_copy(update={"cursor": cursor})
    return settings.model_copy(update={"copilot": copilot, "brain": brain})


def onboard_prompt(probe: dict[str, Any]) -> str:
    return ONBOARD_PROMPT.format(probe=json.dumps(probe, indent=1, ensure_ascii=False))


def refresh_prompt(slot: str, meetings: Path, buddy: str) -> str:
    return REFRESH_PROMPT.format(slot=slot, meetings=meetings.as_posix(), buddy=buddy)


class RefreshSchedule:
    """Which configured time is due now; each runs once a day, across restarts."""

    def __init__(self, times: list[str], state_file: Path, now: Callable[[], datetime] = datetime.now) -> None:
        self.times = times
        self.state_file = state_file
        self._now = now

    def _done(self) -> list[str]:
        try:
            return list(json.loads(self.state_file.read_text(encoding="utf-8")).get("done", []))
        except (OSError, ValueError, AttributeError):
            return []

    def due(self) -> str:
        """The time to run now, marked as run; "" when none is due."""
        now = self._now()
        done = self._done()
        for moment in self.times:
            hour, minute = (int(part) for part in moment.split(":"))
            slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            key = f"{now.date().isoformat()} {moment}"
            if slot <= now < slot + CATCH_UP and key not in done:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(json.dumps({"done": (done + [key])[-20:]}), encoding="utf-8")
                return moment
        return ""
