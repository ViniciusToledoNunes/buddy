"""The user's working memory: what was done, what is in progress, what comes next.

It lives in `memory/` (gitignored), started from the `memory.example/` skeleton, and is
shared: Buddy's brain reads and keeps it, and so do the user's coding agents. The folder
is its own local git repository, so every change can be seen and undone.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import project_root

TEMPLATE = project_root() / "memory.example"
IGNORED = ".DS_Store\n*.tmp\n*.swp\n"


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=directory, capture_output=True, text=True, timeout=20, check=False,
    )


def _identity(directory: Path) -> list[str]:
    """Commits must work on a machine where git has no user configured."""
    if _git(directory, "config", "user.email").stdout.strip():
        return []
    return ["-c", "user.name=Buddy", "-c", "user.email=buddy@localhost"]


def ensure_memory(directory: Path, template: Path = TEMPLATE) -> bool:
    """Create the memory from the skeleton when it is missing; True when it was created."""
    created = not directory.exists()
    if created:
        shutil.copytree(template, directory)
    if shutil.which("git") and not (directory / ".git").exists():
        _git(directory, "init", "-q", "-b", "main")
        ignore = directory / ".gitignore"
        if not ignore.exists():
            ignore.write_text(IGNORED, encoding="utf-8")
        commit_memory(directory, "memory: started")
    return created


def commit_memory(directory: Path, message: str = "") -> str:
    """Commit whatever any agent changed; the message, or "" when there was nothing."""
    if not (directory / ".git").is_dir():
        return ""
    status = _git(directory, "status", "--porcelain")
    changed = [line[3:].strip('"') for line in status.stdout.splitlines() if line.strip()]
    if status.returncode != 0 or not changed:
        return ""
    if not message:
        shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
        message = f"memory: {shown}"
    _git(directory, "add", "-A")
    done = _git(directory, *_identity(directory), "commit", "-q", "-m", message)
    return message if done.returncode == 0 else ""


def memory_prompt(directory: Path, writer: str = "") -> str:
    """What the brain is told about the memory: read always; keep it only when it may write."""
    if not directory.is_dir():
        return ""
    where = directory.as_posix()
    text = (
        f"The user's working memory lives in {where}: global/ (who they are and how they want "
        "things done) and work/ (their current job: the plan, projects, people, decisions). "
        "Before answering, read global/MEMORY.md, work/MEMORY.md and work/project_open_items.md, "
        "then the files that touch the request."
    )
    if writer:
        text += (
            f" Keep it up to date as you work, following {where}/CONVENTIONS.md: record decisions, "
            "the user's corrections and preferences, and changes in the plan (done, in progress, "
            f"next). Use updated_by: {writer}. Never put secrets in it."
        )
    else:
        text += " Do not change it now."
    return text
