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


def copy_skill(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Meeting Copilot skill and MCP registrations")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--client", action="append", choices=("codex", "claude"), default=[])
    args = parser.parse_args()
    root = args.project_root.expanduser().resolve()
    source = root / "skills" / "meeting-copilot-context"
    if not (source / "SKILL.md").exists():
        raise SystemExit(f"Skill source not found: {source}")
    clients = args.client or ["codex", "claude"]
    python = Path(sys.executable).resolve()
    server_args = [str(python), "-m", "meeting_agent.mcp_server"]
    messages: list[str] = []
    if "codex" in clients:
        copy_skill(source, Path.home() / ".codex" / "skills" / source.name)
        messages.append(configure("codex", ["mcp", "add", "meeting-copilot", "--", *server_args], "meeting-copilot"))
    if "claude" in clients:
        copy_skill(source, Path.home() / ".claude" / "skills" / source.name)
        messages.append(configure("claude", ["mcp", "add", "--scope", "user", "meeting-copilot", "--", *server_args], "meeting-copilot"))
    for message in messages:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
