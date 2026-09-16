from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def installed(command: str, name: str) -> bool:
    executable = shutil.which(command)
    if not executable:
        return False
    return subprocess.run([executable, "mcp", "get", name], capture_output=True).returncode == 0


def configure(command: str, args: list[str], name: str) -> str:
    executable = shutil.which(command)
    if not executable:
        return f"{command}: CLI not found; skill copied, MCP registration skipped"
    if installed(command, name):
        return f"{command}: MCP server '{name}' already exists; left unchanged"
    result = subprocess.run([executable, *args], capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{command} MCP registration failed: {detail}")
    return f"{command}: skill installed and MCP server registered"


def copy_skill(source: Path, destination: Path, command: str = "buddy") -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    # The skill names the Buddy executable by path: a Claude Code shell rarely has the
    # project's virtual environment on PATH.
    skill = destination / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    if "BUDDY_COMMAND" in text:
        skill.write_text(text.replace("BUDDY_COMMAND", command), encoding="utf-8")


def buddy_command(python: Path) -> str:
    name = "buddy.exe" if sys.platform == "win32" else "buddy"
    candidate = python.with_name(name)
    return candidate.as_posix() if candidate.exists() else "buddy"


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Buddy skill and MCP registrations")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--client", action="append", choices=("codex", "claude"), default=[])
    args = parser.parse_args()
    root = args.project_root.expanduser().resolve()
    source = root / "skills" / "meeting-copilot-context"
    listener = root / "skills" / "buddy-listener"
    for skill in (source, listener):
        if not (skill / "SKILL.md").exists():
            raise SystemExit(f"Skill source not found: {skill}")
    clients = args.client or ["codex", "claude"]
    python = Path(sys.executable).resolve()
    command = buddy_command(python)
    server_args = [str(python), "-m", "meeting_agent.mcp_server"]
    messages: list[str] = []
    if "codex" in clients:
        copy_skill(source, Path.home() / ".codex" / "skills" / source.name)
        messages.append(configure("codex", ["mcp", "add", "buddy", "--", *server_args], "buddy"))
    if "claude" in clients:
        copy_skill(source, Path.home() / ".claude" / "skills" / source.name, command)
        # The listener skill drives a Claude Code Monitor, which Codex does not have.
        copy_skill(listener, Path.home() / ".claude" / "skills" / listener.name, command)
        messages.append(configure("claude", ["mcp", "add", "--scope", "user", "buddy", "--", *server_args], "buddy"))
    for message in messages:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
