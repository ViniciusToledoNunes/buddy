from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import load_settings
from .context import MeetingRepository, SpeakerFilter


def create_server(repository: MeetingRepository | None = None) -> MCPServer:
    repo = repository or MeetingRepository.from_settings(load_settings())
    server = MCPServer(
        "Meeting Copilot",
        version="0.2.0",
        instructions=(
            "Read live or saved Meeting Copilot transcripts and connect them to the host agent's project context. "
            "Never infer identities beyond ME and REMOTE. Recording must only be started after explicit user consent."
        ),
    )

    @server.tool(description="Return whether Meeting Copilot is recording and its current session metadata.")
    def meeting_status() -> dict[str, Any]:
        return repo.status()

    @server.tool(description="List recent saved meetings, newest first. Returns at most 50 entries.")
    def list_meetings(limit: int = 10) -> list[dict[str, Any]]:
        return repo.list_meetings(limit)

    @server.tool(description="Read a bounded window of the active meeting transcript.")
    def get_live_transcript(minutes: int = 5, max_events: int = 200, speaker: SpeakerFilter = "all") -> dict[str, Any]:
        return repo.live_transcript(minutes, max_events, speaker)

    @server.tool(description="Read metadata, report, copilot memory, and optionally transcript for one saved meeting.")
    def get_meeting(meeting_id: str = "latest", include_transcript: bool = True, max_events: int = 500) -> dict[str, Any]:
        try:
            return repo.meeting(meeting_id, include_transcript, max_events)
        except ValueError as exc:
            return {"error": {"code": "meeting_not_found", "message": str(exc)}}

    @server.tool(description="Search bounded transcript history for an exact case-insensitive text fragment.")
    def search_meetings(query: str, limit: int = 20) -> dict[str, Any]:
        try:
            return {"query": query, "results": repo.search(query, limit)}
        except ValueError as exc:
            return {"error": {"code": "invalid_query", "message": str(exc)}}

    @server.tool(description="Read compact decisions, actions, questions, and recent suggestions for a meeting.")
    def get_copilot_context(meeting_id: str = "latest") -> dict[str, Any]:
        try:
            return repo.copilot_context(meeting_id)
        except ValueError as exc:
            return {"error": {"code": "meeting_not_found", "message": str(exc)}}

    @server.tool(description="Ask the running copilot to generate a suggestion from its current transcript window.")
    def request_suggestion() -> dict[str, Any]:
        return repo.request_suggestion()

    @server.tool(description="Stop an active recording immediately and allow its final report to be generated.")
    def stop_meeting() -> dict[str, Any]:
        return repo.request_stop()

    @server.tool(description="Start recording only after explicit confirmation and administrator opt-in.")
    def start_meeting(confirmed: bool = False) -> dict[str, Any]:
        allowed = os.getenv("MEETING_COPILOT_ALLOW_MCP_START", "").lower() in {"1", "true", "yes"}
        if not confirmed:
            return {"accepted": False, "reason": "explicit confirmation is required"}
        if not allowed:
            return {"accepted": False, "reason": "MCP start is disabled; set MEETING_COPILOT_ALLOW_MCP_START=true"}
        if repo.status()["active"]:
            return {"accepted": False, "reason": "a meeting is already active"}
        kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen([sys.executable, "-m", "meeting_agent.cli", "start"], **kwargs)
        return {"accepted": True, "pid": process.pid}

    @server.resource("meeting://status", description="Current Meeting Copilot state as JSON.")
    def status_resource() -> str:
        return json.dumps(repo.status(), ensure_ascii=False)

    @server.resource("meeting://recent", description="Ten most recent meetings as JSON.")
    def recent_resource() -> str:
        return json.dumps(repo.list_meetings(10), ensure_ascii=False)

    @server.resource("meeting://latest/summary", description="Latest saved or active meeting summary.")
    def latest_summary_resource() -> str:
        try:
            return str(repo.meeting("latest", include_transcript=False).get("summary", ""))
        except ValueError:
            return "No meetings are available."

    @server.prompt(description="Analyze meeting context against the current project using host-agent tools.")
    def relate_meeting_to_project(project_name: str = "the current project") -> str:
        return (
            f"Read the bounded live transcript and copilot context, then inspect {project_name} with native project tools. "
            "Connect only concrete meeting terms, questions, decisions, risks, or action items to evidence in the project. "
            "State uncertainty, protect secrets, and keep any speakable suggestion concise."
        )

    return server


mcp = create_server()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
